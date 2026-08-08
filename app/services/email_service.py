"""
Transactional email.

Three providers, selected by EMAIL_PROVIDER:

  resend   — production. Needs EMAIL_API_KEY and a domain verified with Resend.
  smtp     — any SMTP host. Points at a local catcher (Mailpit/MailHog on
             localhost:1025) by default, so local runs can exercise the real
             render-and-send path without an account or a verified domain.
             Real hosts pick a TLS mode by port: 587 + SMTP_STARTTLS, or
             465 + SMTP_SSL. See _send_smtp.
  console  — no network at all. Renders the message to EMAIL_OUTBOX_DIR as a
             timestamped .html and logs a one-line summary.

Sends are best-effort by design: callers are BackgroundTasks on user-facing
flows (placement-test results, gift activation), and a mail outage must not fail
the request that triggered it. Every path returns {"id", "status"} and logs
rather than raising — but an unsendable configuration is now logged loudly at
import, instead of failing silently on the first send as it did before.
"""

import asyncio
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.config import settings

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "email"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)

# Every template needs these for its links and footer, but callers pass template
# data ad hoc and several were omitting them — an undefined name renders as ""
# in Jinja, so the result was a live <a href=""> pointing at nothing rather than
# an error. As globals they are always present; anything in template_data still
# takes precedence, since render() context beats env globals.
_jinja_env.globals.update(
    web_base_url=settings.web_base_url,
    api_base_url=settings.api_base_url,
)

_BACKEND_ROOT = Path(__file__).parent.parent.parent

_VALID_PROVIDERS = ("resend", "smtp", "console")


def _provider() -> str:
    p = (settings.email_provider or "console").strip().lower()
    if p not in _VALID_PROVIDERS:
        logger.error(
            "EMAIL_PROVIDER=%r is not one of %s — falling back to 'console'. "
            "(SendGrid is named in the docs but has never been implemented here.)",
            settings.email_provider, _VALID_PROVIDERS,
        )
        return "console"
    if p == "resend" and not settings.email_api_key:
        logger.error(
            "EMAIL_PROVIDER=resend but EMAIL_API_KEY is empty — no mail can be "
            "sent. Set EMAIL_API_KEY, or use EMAIL_PROVIDER=console for local work."
        )
    if p == "smtp" and settings.smtp_port == 465 and not settings.smtp_ssl:
        # Caught here because the failure mode is otherwise a silent 15s stall
        # per send rather than a refused connection.
        logger.error(
            "SMTP_PORT=465 is implicit TLS but SMTP_SSL is false — every send "
            "will hang until the timeout. Set SMTP_SSL=true, or use port 587 "
            "with SMTP_STARTTLS=true."
        )
    return p


def _failed(reason: str) -> dict:
    logger.error("Email send failed: %s", reason)
    return {"id": "", "status": "failed"}


# ── Providers ────────────────────────────────────────────────────────────────

def _send_resend(recipients: list[str], subject: str, html_body: str,
                 text_body: str | None, reply_to: str | None,
                 tags: list[str] | None) -> dict:
    """Blocking — always called via asyncio.to_thread."""
    import resend

    # Set per-send rather than at import: reading the key at module load froze
    # whatever was in the environment at the time and made the key untestable.
    resend.api_key = settings.email_api_key

    params: dict = {
        "from": settings.email_from_address,
        "to": recipients,
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        params["text"] = text_body
    if reply_to:
        params["reply_to"] = reply_to
    if tags:
        params["tags"] = [{"name": t, "value": "true"} for t in tags]

    response = resend.Emails.send(params)
    return {"id": (response or {}).get("id", ""), "status": "sent"}


def _send_smtp(recipients: list[str], subject: str, html_body: str,
               text_body: str | None, reply_to: str | None) -> dict:
    """Blocking — always called via asyncio.to_thread."""
    import smtplib

    msg = EmailMessage()
    msg["From"] = settings.email_from_address
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    # A text/plain alternative must exist before add_alternative(html), or
    # non-HTML clients get an empty body.
    msg.set_content(text_body or "This message requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    # The two TLS modes are not interchangeable. Implicit TLS (port 465) expects
    # a handshake on the first byte, so plain SMTP sends a cleartext EHLO into a
    # TLS listener and blocks until the timeout; STARTTLS (port 587) is the
    # opposite — connect in the clear, then upgrade. SMTP_SSL picks the former,
    # in which case STARTTLS is not offered and must not be attempted.
    smtp_class = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP

    with smtp_class(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls and not settings.smtp_ssl:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)

    return {"id": f"smtp:{datetime.now(timezone.utc).timestamp()}", "status": "sent"}


def _send_console(recipients: list[str], subject: str, html_body: str) -> dict:
    """Blocking — always called via asyncio.to_thread."""
    outbox = Path(settings.email_outbox_dir)
    if not outbox.is_absolute():
        outbox = _BACKEND_ROOT / outbox
    outbox.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_subject = "".join(c if c.isalnum() or c in "-_ " else "_" for c in subject)[:60].strip()
    path = outbox / f"{stamp}__{safe_subject or 'message'}.html"

    header = (
        f"<!-- To: {', '.join(recipients)}\n"
        f"     From: {settings.email_from_address}\n"
        f"     Subject: {subject}\n"
        f"     Rendered: {datetime.now(timezone.utc).isoformat()} -->\n"
    )
    path.write_text(header + html_body, encoding="utf-8")

    logger.info("[email:console] to=%s subject=%r -> %s", recipients, subject, path)
    return {"id": f"console:{stamp}", "status": "sent"}


# ── Public API ───────────────────────────────────────────────────────────────

async def send_email(
    to: str | list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Send transactional email via the configured provider."""
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [r for r in recipients if r]
    if not recipients:
        return _failed("no recipients")

    provider = _provider()

    try:
        # Every provider below does blocking network or disk I/O. These run on
        # the event loop (BackgroundTasks on an async app), so a slow or hanging
        # mail host would stall every other in-flight request.
        if provider == "resend":
            if not settings.email_api_key:
                return _failed("EMAIL_API_KEY is not set")
            return await asyncio.to_thread(
                _send_resend, recipients, subject, html_body, text_body, reply_to, tags
            )
        if provider == "smtp":
            return await asyncio.to_thread(
                _send_smtp, recipients, subject, html_body, text_body, reply_to
            )
        return await asyncio.to_thread(_send_console, recipients, subject, html_body)
    except Exception as exc:
        return _failed(f"provider={provider} {type(exc).__name__}: {exc}")


async def send_template_email(
    to: str,
    template_name: str,
    template_data: dict,
    tags: list[str] | None = None,
) -> dict:
    """Render a Jinja2 email template and send it."""
    try:
        template = _jinja_env.get_template(f"{template_name}.html")
        html_body = template.render(**template_data)
    except Exception as exc:
        return _failed(f"template={template_name} {type(exc).__name__}: {exc}")

    subject = template_data.get("subject", "Tauka")
    return await send_email(to, subject, html_body, tags=tags)
