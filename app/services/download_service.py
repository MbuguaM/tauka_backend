"""Installers behind sign-in (§155, D15, developer 2026-11-04: nobody downloads without an account).

The `downloads` bucket is private with no client policy, so only this service can read it.

WEBSITE  a signed-in person asks for a platform and gets a signed link that lasts 5 minutes.
UPDATES  WinSparkle and Sparkle can't send a login, so a signed-in app asks for a feed address
         carrying a token tied to that user for 7 days. The feed is `appcast.xml` from the
         bucket with each installer swapped for a signed link. Update signatures are checked
         against the key built into the app, so this changes who can fetch, not what's trusted.

Which installer is current is set by DOWNLOAD_PATH_WINDOWS, _MACOS and _ANDROID, so a release
changes a setting and no client. An empty setting means that platform isn't available.
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import quoteattr

from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BUCKET = "downloads"
APPCAST = "appcast.xml"
LINK_SECONDS = 300
ENCLOSURE_SECONDS = 3600
FEED_LIFETIME = timedelta(days=7)
_ENCLOSURE_URL = re.compile(r"""(<enclosure\b[^>]*?\burl=)("[^"]*"|'[^']*')""")


class DownloadRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _paths() -> dict[str, str]:
    return {"windows": settings.download_path_windows, "macos": settings.download_path_macos,
            "android": settings.download_path_android}


def availability() -> dict[str, bool]:
    """Which platforms can be downloaded. Public, and it holds no address."""
    return {platform: bool(path) for platform, path in _paths().items()}


def _signed(path: str, seconds: int) -> str:
    try:
        res = db.storage.from_(BUCKET).create_signed_url(path, seconds, {"download": True})
    except Exception as exc:  # noqa: BLE001
        logger.error("downloads: could not sign %s: %s", path, exc)
        raise DownloadRefused("unavailable") from exc
    url = res.get("signedURL") or res.get("signedUrl") if isinstance(res, dict) else None
    if not url:
        logger.error("downloads: no signed link for %s", path)
        raise DownloadRefused("unavailable")
    return url


def link(platform: str) -> dict:
    """A short-lived link for someone already signed in. The route checks that."""
    path = _paths().get(platform)
    if not path:
        raise DownloadRefused("not_available")
    return {"url": _signed(path, LINK_SECONDS), "expires_in": LINK_SECONDS}


def _sign(payload: str) -> str:
    return hmac.new(settings.download_token_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def feed_token(user_id: str, now: datetime | None = None) -> str:
    if not settings.download_token_secret:
        raise DownloadRefused("not_configured")
    expires = int(((now or datetime.now(timezone.utc)) + FEED_LIFETIME).timestamp())
    payload = f"{expires}.{user_id}"
    return f"{payload}.{_sign(payload)}"


def feed_url(user_id: str) -> str:
    return f"{settings.api_base_url.rstrip('/')}/downloads/appcast.xml?token={feed_token(user_id)}"


def token_ok(token: str, now: datetime | None = None) -> bool:
    if not settings.download_token_secret:
        return False
    payload, _, signature = (token or "").rpartition(".")
    if not payload or not hmac.compare_digest(_sign(payload), signature):
        return False
    try:
        return int(payload.partition(".")[0]) > (now or datetime.now(timezone.utc)).timestamp()
    except ValueError:
        return False


def signed_appcast(raw: str) -> str:
    """Swap each installer for a signed link. Only the file name is used, so the feed can't
    name anything outside this bucket's top level."""
    def swap(match: re.Match) -> str:
        name = match.group(2)[1:-1].rsplit("/", 1)[-1]
        return f"{match.group(1)}{quoteattr(_signed(name, ENCLOSURE_SECONDS))}"
    return _ENCLOSURE_URL.sub(swap, raw)


def appcast(token: str) -> str:
    if not token_ok(token):
        raise DownloadRefused("bad_token")
    try:
        raw = db.storage.from_(BUCKET).download(APPCAST)
    except Exception as exc:  # noqa: BLE001
        logger.error("downloads: could not read the feed: %s", exc)
        raise DownloadRefused("unavailable") from exc
    return signed_appcast(raw.decode("utf-8"))
