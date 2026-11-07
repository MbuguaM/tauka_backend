"""Exchange cases, sanctions, appeals and disputes (§159; exchange-extension-plan XE-11,
XE-12, XE-20; exchange doc 03).

Cases are `concern_report` rows: the existing safeguarding queue, readable only by the
reporter and the safeguarding capability, never ops and never the person named (§117,
L-015). This adds severity S1-S4 with response deadlines, an interim suspension for S3/S4,
sanctions with expiring strikes and the escalation of doc 03 §7, one appeal per decision,
and disputes (attendance, ratings, pay...) with the session's evidence attached.

Behind `exchange_cases`: with it off a report is filed exactly as before (no severity, no
suspension), so nothing changes until safeguarding is ready to work the new queue.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# Doc 03 §4: each report category's default severity. Triage can change it.
SEVERITY = {
    "underage": "S4",
    "sexual_conduct": "S3", "hate_speech": "S3", "threat": "S3", "fraud": "S3", "harassment": "S3", "safety": "S3",
    "contact_details": "S2", "solicitation": "S2", "account_sharing": "S2", "inappropriate_content": "S2",
    "wellbeing": "S2", "other": "S2",
}
# Doc 03 §6.1: (first action, decision) deadlines.
DEADLINES = {
    "S4": (timedelta(hours=1), timedelta(hours=72)),
    "S3": (timedelta(hours=12), timedelta(days=7)),
    "S2": (timedelta(hours=72), timedelta(days=14)),
    "S1": (timedelta(days=7), timedelta(days=14)),
}
SANCTIONS = ("nudge", "warning", "strike", "suspend_7", "suspend_30", "suspend_90", "ban", "role_removal")
STRIKE_LIFE = timedelta(days=180)
APPEAL_WITHIN = timedelta(days=14)
CONTACT_ATTEMPTS = 3                     # XE-20: in 30 days opens an S2 case
DISPUTE_KINDS = ("attendance", "credits", "rating", "quality", "placement", "pay")
DISPUTE_DUE = {1: timedelta(days=7), 2: timedelta(days=14), 3: timedelta(days=14)}
PATTERN_REPORTS = 3                      # doc 03 §6.6: several S1/S2 in 30 days are reviewed together


class CaseRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _safeguarding(actor: str) -> None:
    if not hs.admin_can(actor, "safeguarding"):
        raise CaseRefused("not_allowed")


def _ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise CaseRefused("not_allowed")


# ── triage (on every exchange report) ──────────────────────────────────────

def triage(report: dict, now: datetime | None = None) -> dict:
    """Give a new exchange report its severity and deadlines; S3/S4 suspend the person
    named while the case is looked at (no fault implied, doc 03 §6.2)."""
    if not is_ready("exchange_cases"):
        return report
    now = now or _now()
    sev = SEVERITY.get(report.get("category"), "S2")
    first, decide = DEADLINES[sev]
    patch = {"severity": sev, "first_action_due": (now + first).isoformat(), "decision_due": (now + decide).isoformat()}
    if sev in ("S3", "S4") and report.get("about_user_id"):
        interim(report["about_user_id"], report["id"], now)
        patch["interim_suspended_at"] = now.isoformat()
    _t("concern_report").update(patch).eq("id", report["id"]).execute()
    return {**report, **patch}


def interim(user_id: str, concern_id: str | None, now: datetime) -> None:
    """Out of matching, future sessions cancelled without penalty to anyone."""
    _t("exchange_sanction").insert({"user_id": user_id, "kind": "interim", "concern_id": concern_id,
                                    "reason": "Interim suspension while a report is reviewed"}).execute()
    p = (_t("exchange_partner").select("user_id, status").eq("user_id", user_id).limit(1).execute()).data or []
    if p and p[0]["status"] == "active":
        _t("exchange_partner").update({"status": "paused", "status_reason": "interim_suspension",
                                       "updated_at": now.isoformat()}).eq("user_id", user_id).execute()
    for side in ("learner_id", "partner_id"):
        for s in (_t("exchange_session").select("id, status, scheduled_at").eq(side, user_id)
                  .in_("status", ["requested", "booked"]).execute()).data or []:
            if _ts(s["scheduled_at"]) > now:
                _t("exchange_session").update({"status": "cancelled", "cancelled_at": now.isoformat(),
                                               "cancel_reason": "interim", "late_cancel": False}).eq("id", s["id"]).execute()
    for m in (_t("exchange_group_member").select("id, status, group_id").eq("user_id", user_id).eq("status", "booked")
              .execute()).data or []:
        _t("exchange_group_member").update({"status": "cancelled"}).eq("id", m["id"]).execute()


def suspended(user_id: str, now: datetime | None = None) -> bool:
    """An interim suspension on an open case, a running suspension, or a ban."""
    now = now or _now()
    try:
        rows = (_t("exchange_sanction").select("kind, ends_at, lifted_at, concern_id").eq("user_id", user_id)
                .execute()).data or []
    except Exception:  # noqa: BLE001
        return False
    for r in rows:
        if r.get("lifted_at"):
            continue
        if r["kind"] == "ban":
            return True
        if r["kind"] in ("suspend_7", "suspend_30", "suspend_90") and r.get("ends_at") and _ts(r["ends_at"]) > now:
            return True
        if r["kind"] == "interim":
            return True
    return False


# ── the safeguarding queue: decide, sanction, lift ─────────────────────────

def cases(actor: str) -> list[dict]:
    """Exchange cases, most urgent first (the concern queue shows them too)."""
    _safeguarding(actor)
    rows = (_t("concern_report").select("*").execute()).data or []
    rows = [r for r in rows if r.get("session_type") == "exchange_session" or r.get("automatic") or r.get("severity")]
    order = {"S4": 0, "S3": 1, "S2": 2, "S1": 3, None: 4}
    return sorted(rows, key=lambda r: (r["status"] == "closed", order.get(r.get("severity")), str(r.get("created_at") or "9999")))


def set_severity(actor: str, concern_id: str, severity: str, now: datetime | None = None) -> dict:
    _safeguarding(actor)
    if severity not in DEADLINES:
        raise CaseRefused("bad_value")
    now = now or _now()
    row = _case(concern_id)
    first, decide = DEADLINES[severity]
    patch = {"severity": severity, "first_action_due": (_ts(row.get("created_at") or now) + first).isoformat(),
             "decision_due": (_ts(row.get("created_at") or now) + decide).isoformat()}
    if severity in ("S3", "S4") and row.get("about_user_id") and not row.get("interim_suspended_at"):
        interim(row["about_user_id"], concern_id, now)
        patch["interim_suspended_at"] = now.isoformat()
    return (_t("concern_report").update(patch).eq("id", concern_id).execute()).data[0]


def _case(concern_id: str) -> dict:
    rows = (_t("concern_report").select("*").eq("id", concern_id).limit(1).execute()).data or []
    if not rows:
        raise CaseRefused("no_such_case")
    return rows[0]


def _active_strikes(user_id: str, now: datetime) -> int:
    return sum(1 for r in (_t("exchange_sanction").select("kind, strike_expires_at, lifted_at").eq("user_id", user_id)
               .execute()).data or [] if r["kind"] == "strike" and not r.get("lifted_at")
               and r.get("strike_expires_at") and _ts(r["strike_expires_at"]) > now)


def sanction(actor: str, user_id: str, kind: str, concern_id: str | None, reason: str | None,
             now: datetime | None = None) -> list[dict]:
    """Doc 03 §7. A strike can escalate: 2 active = 7 days off, 3 = 30 days, a 4th = a ban."""
    _safeguarding(actor)
    if kind not in SANCTIONS:
        raise CaseRefused("bad_value")
    now = now or _now()
    made = [_sanction(user_id, kind, concern_id, reason, actor, now)]
    if kind == "strike":
        n = _active_strikes(user_id, now)
        follow = "ban" if n >= 4 else "suspend_30" if n == 3 else "suspend_7" if n == 2 else None
        if follow:
            made.append(_sanction(user_id, follow, concern_id, f"{n} active strikes", actor, now))
    # A decision ends the interim suspension it replaces.
    for r in (_t("exchange_sanction").select("id, kind, lifted_at, concern_id").eq("user_id", user_id).eq("kind", "interim")
              .execute()).data or []:
        if not r.get("lifted_at") and (concern_id is None or r.get("concern_id") == concern_id):
            _t("exchange_sanction").update({"lifted_at": now.isoformat()}).eq("id", r["id"]).execute()
    _apply_partner_status(user_id, now)
    return made


def _sanction(user_id: str, kind: str, concern_id: str | None, reason: str | None, actor: str, now: datetime) -> dict:
    row = {"user_id": user_id, "kind": kind, "concern_id": concern_id, "reason": (reason or "")[:1000] or None,
           "decided_by": actor, "starts_at": now.isoformat()}
    if kind == "strike":
        row["strike_expires_at"] = (now + STRIKE_LIFE).isoformat()
    days = {"suspend_7": 7, "suspend_30": 30, "suspend_90": 90}.get(kind)
    if days:
        row["ends_at"] = (now + timedelta(days=days)).isoformat()
    return (_t("exchange_sanction").insert(row).execute()).data[0]


def _apply_partner_status(user_id: str, now: datetime) -> None:
    """A partner banned or with their role removed is removed; suspended is paused; otherwise
    a pause that came from an interim suspension ends."""
    p = (_t("exchange_partner").select("user_id, status, status_reason").eq("user_id", user_id).limit(1).execute()).data or []
    if not p:
        return
    rows = [r for r in (_t("exchange_sanction").select("kind, ends_at, lifted_at").eq("user_id", user_id).execute()).data or []
            if not r.get("lifted_at")]
    if any(r["kind"] in ("ban", "role_removal") for r in rows):
        patch = {"status": "removed", "status_reason": "sanction"}
    elif suspended(user_id, now):
        patch = {"status": "paused", "status_reason": "suspended"}
    elif p[0]["status"] == "paused" and p[0].get("status_reason") in ("interim_suspension", "suspended"):
        patch = {"status": "active", "status_reason": None}
    else:
        return
    _t("exchange_partner").update({**patch, "updated_at": now.isoformat()}).eq("user_id", user_id).execute()


def lift(actor: str, sanction_id: str, now: datetime | None = None) -> dict:
    _safeguarding(actor)
    now = now or _now()
    rows = (_t("exchange_sanction").update({"lifted_at": now.isoformat()}).eq("id", sanction_id).execute()).data or []
    if not rows:
        raise CaseRefused("no_such_sanction")
    _apply_partner_status(rows[0]["user_id"], now)
    return rows[0]


# ── appeals (doc 03 §10) ───────────────────────────────────────────────────

def my_sanctions(user_id: str) -> list[dict]:
    """What the person named may see: the sanction and its reason, never who reported."""
    rows = (_t("exchange_sanction").select("id, kind, reason, starts_at, ends_at, strike_expires_at, lifted_at, created_at")
            .eq("user_id", user_id).execute()).data or []
    appeals = {a["sanction_id"]: a for a in (_t("exchange_appeal").select("sanction_id, status").eq("user_id", user_id)
               .execute()).data or []}
    return [{**r, "appeal": appeals.get(r["id"], {}).get("status")} for r in rows if r["kind"] not in ("interim", "nudge")]


def appeal(user_id: str, sanction_id: str, statement: str, now: datetime | None = None) -> dict:
    now = now or _now()
    rows = (_t("exchange_sanction").select("*").eq("id", sanction_id).limit(1).execute()).data or []
    if not rows or rows[0]["user_id"] != user_id:
        raise CaseRefused("no_such_sanction")
    if rows[0]["kind"] in ("nudge", "interim"):
        raise CaseRefused("not_appealable")
    if now - _ts(rows[0].get("created_at") or rows[0]["starts_at"]) > APPEAL_WITHIN:
        raise CaseRefused("too_late")
    try:
        return (_t("exchange_appeal").insert({"sanction_id": sanction_id, "user_id": user_id, "status": "open",
                                              "statement": statement.strip()[:2000],
                                              "due_at": (now + timedelta(days=14)).isoformat()}).execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise CaseRefused("already_appealed") from exc
        raise


def decide_appeal(actor: str, appeal_id: str, outcome: str, now: datetime | None = None) -> dict:
    """Someone who didn't make the original decision. Overturned lifts the sanction."""
    _safeguarding(actor)
    if outcome not in ("upheld", "reduced", "overturned"):
        raise CaseRefused("bad_value")
    now = now or _now()
    rows = (_t("exchange_appeal").select("*").eq("id", appeal_id).limit(1).execute()).data or []
    if not rows or rows[0]["status"] != "open":
        raise CaseRefused("not_open")
    s = (_t("exchange_sanction").select("*").eq("id", rows[0]["sanction_id"]).limit(1).execute()).data[0]
    if s.get("decided_by") == actor:
        raise CaseRefused("conflict_of_interest")
    if outcome == "overturned":
        _t("exchange_sanction").update({"lifted_at": now.isoformat()}).eq("id", s["id"]).execute()
    elif outcome == "reduced" and s.get("ends_at"):
        half = _ts(s["starts_at"]) + (_ts(s["ends_at"]) - _ts(s["starts_at"])) / 2
        _t("exchange_sanction").update({"ends_at": half.isoformat()}).eq("id", s["id"]).execute()
    _apply_partner_status(s["user_id"], now)
    return (_t("exchange_appeal").update({"status": outcome, "decided_by": actor, "decided_at": now.isoformat()})
            .eq("id", appeal_id).execute()).data[0]


def appeals(actor: str) -> list[dict]:
    _safeguarding(actor)
    return (_t("exchange_appeal").select("*").eq("status", "open").execute()).data or []


# ── XE-20: contact-detail attempts ─────────────────────────────────────────

async def contact_attempts(sender_id: str, now: datetime | None = None) -> bool:
    """Three attempts in 30 days open one automatic S2 case (then nothing more for 30 days)."""
    now = now or _now()
    since = (now - timedelta(days=30)).isoformat()
    n = sum(1 for r in (_t("exchange_message_flag").select("kind, created_at").eq("sender_id", sender_id)
            .eq("kind", "contact_attempt").execute()).data or [] if str(r.get("created_at") or "9999") >= since)
    if n < CONTACT_ATTEMPTS:
        return False
    recent = [r for r in (_t("concern_report").select("id, automatic, about_user_id, created_at, category")
              .eq("about_user_id", sender_id).execute()).data or []
              if r.get("automatic") and r.get("category") == "contact_details" and str(r.get("created_at") or "9999") >= since]
    if recent:
        return False
    is_partner = bool((_t("exchange_partner").select("user_id").eq("user_id", sender_id).limit(1).execute()).data)
    row = (_t("concern_report").insert({
        "reporter_id": None, "automatic": True, "about_kind": "partner" if is_partner else "learner",
        "about_user_id": sender_id, "category": "contact_details",
        "detail": f"{n} attempts to share contact details in exchange chats in 30 days (automatic)."}).execute()).data[0]
    triage(row, now)
    try:
        from app.services.concern_service import alert_new
        await alert_new(row["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("contact-attempt case alert failed: %s", exc)
    return True


# ── disputes (doc 03 §9) ───────────────────────────────────────────────────

def _evidence(session_id: str | None) -> dict:
    """What the system recorded about the session: its status and joins, and the call
    provider's presence intervals (XE-10). Attached automatically."""
    if not session_id:
        return {}
    s = (_t("exchange_session").select("id, status, scheduled_at, length_min, learner_joined_at, partner_joined_at, "
                                        "missed_by, cancelled_by, late_cancel, excused_reason, connect_failures")
         .eq("id", session_id).limit(1).execute()).data or []
    presence = (_t("exchange_presence").select("user_id, provider, joined_at, left_at").eq("session_id", session_id)
                .execute()).data or []
    return {"session": s[0] if s else None, "presence": presence}


def open_dispute(user_id: str, kind: str, session_id: str | None, statement: str, now: datetime | None = None) -> dict:
    if kind not in DISPUTE_KINDS:
        raise CaseRefused("bad_value")
    now = now or _now()
    if session_id:
        s = (_t("exchange_session").select("learner_id, partner_id").eq("id", session_id).limit(1).execute()).data or []
        if not s or user_id not in (s[0]["learner_id"], s[0]["partner_id"]):
            raise CaseRefused("not_your_session")
    return (_t("exchange_dispute").insert({
        "user_id": user_id, "kind": kind, "session_id": session_id, "statement": statement.strip()[:2000],
        "evidence": _evidence(session_id), "level": 1, "status": "open",
        "due_at": (now + DISPUTE_DUE[1]).isoformat()}).execute()).data[0]


def my_disputes(user_id: str) -> list[dict]:
    return (_t("exchange_dispute").select("id, kind, session_id, level, status, decision, due_at, created_at")
            .eq("user_id", user_id).execute()).data or []


def disputes(actor: str) -> list[dict]:
    _ops(actor)
    return sorted((_t("exchange_dispute").select("*").neq("status", "resolved").execute()).data or [],
                  key=lambda r: str(r["due_at"]))


def decide_dispute(actor: str, dispute_id: str, action: str, decision: str, now: datetime | None = None) -> dict:
    """resolve, or escalate to the next level (L2 a senior, L3 final). For a rating dispute,
    `remove_rating` drops a retaliatory rating (doc 03 §9.2)."""
    _ops(actor)
    now = now or _now()
    rows = (_t("exchange_dispute").select("*").eq("id", dispute_id).limit(1).execute()).data or []
    if not rows or rows[0].get("status", "open") == "resolved":
        raise CaseRefused("not_open")
    d = {"level": 1, "status": "open", **rows[0]}
    if d.get("decided_by") == actor and action == "escalate":
        raise CaseRefused("conflict_of_interest")
    if action == "escalate":
        if d["level"] >= 3:
            raise CaseRefused("final")
        level = d["level"] + 1
        patch = {"level": level, "status": "escalated", "decided_by": actor, "decision": decision[:2000],
                 "due_at": (now + DISPUTE_DUE[level]).isoformat()}
    elif action in ("resolve", "remove_rating"):
        if action == "remove_rating":
            if d["kind"] != "rating" or not d.get("session_id"):
                raise CaseRefused("bad_value")
            _t("exchange_rating").delete().eq("session_id", d["session_id"]).neq("rater_id", d["user_id"]).execute()
        patch = {"status": "resolved", "decided_by": actor, "decided_at": now.isoformat(), "decision": decision[:2000]}
    else:
        raise CaseRefused("bad_value")
    return (_t("exchange_dispute").update(patch).eq("id", dispute_id).execute()).data[0]


# ── daily: patterns, overdue cases ─────────────────────────────────────────

async def daily(now: datetime | None = None) -> dict:
    """Doc 03 §6.6: several S1/S2 reports about one person in 30 days are flagged to
    safeguarding as one; cases past their first-action deadline are flagged too."""
    if not is_ready("exchange_cases"):
        return {"enabled": False}
    now = now or _now()
    since = (now - timedelta(days=30)).isoformat()
    rows = (_t("concern_report").select("id, about_user_id, severity, status, created_at, first_action_due, acknowledged_at")
            .execute()).data or []
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("about_user_id") and r.get("severity") in ("S1", "S2") and str(r.get("created_at") or "9999") >= since:
            counts[r["about_user_id"]] = counts.get(r["about_user_id"], 0) + 1
    patterns = [u for u, n in counts.items() if n >= PATTERN_REPORTS]
    overdue = [r["id"] for r in rows if r["status"] == "open" and r.get("first_action_due")
               and _ts(r["first_action_due"]) < now]
    if patterns or overdue:
        try:
            from app.config import settings
            from app.services import email_service
            from app.services.concern_service import _safeguarding_emails
            to = _safeguarding_emails()
            if to:
                await email_service.send_email(
                    to, "Exchange cases need a look",
                    f"<p>{len(patterns)} people with several reports in 30 days, {len(overdue)} cases past their "
                    f"first-action deadline.</p><p><a href=\"{settings.web_base_url}/admin/exchange\">Open the cases</a></p>",
                    tags=["exchange", "cases"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("case digest failed: %s", exc)
    return {"patterns": len(patterns), "overdue": len(overdue)}
