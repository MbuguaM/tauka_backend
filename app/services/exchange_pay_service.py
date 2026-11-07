"""Paid exchange partners: rates, earnings, holds, weekly batches, payouts (§159;
exchange-extension-plan XE-24; exchange doc 02 §4-§5).

Nothing here pays anyone until counsel clears the payee category (gate G1) and the partner
agreement (G2): it is all behind `paid_partners`, and it only ever applies to partners ops
gave the 'pap' (paid Amharic partner) or 'fep' (fluent English partner) role. Today's barter
partners and their data top-ups (exchange_reward) are not touched.

Money is integer minor units (KES cents). An earning is never edited into a different
amount: a correction is a `reversal` row, netted in the next batch, never a negative payout.

    held (7 days) -> releasable -> batched -> paid           (an open case keeps it held)

A batch is built on Fridays (07:00 UTC = 10:00 EAT), approved by two different staff, then
sent. Sending needs a payout provider: Safaricom B2C (a B2C shortcode, initiator and security
credential, and Safaricom's approval) or an airtime/data vendor. Neither is configured, so a
batch can be built and approved, and sending marks each payout `failed: not_configured`.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.core.flags import is_ready
from app.services import hierarchy_service as hs
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

HOLD = timedelta(days=7)
MIN_PAYOUT_MINOR = 50000                  # KES 500
COOLDOWN = timedelta(days=7)
NO_SHOW_PROTECTION_CAP = 6                # a month (doc 02 §4.4)
PAID_ROLES = ("pap", "fep")
# Illustrative until set (doc 02 §4.1); ops replace them with POST /exchange/admin/rates.
DEFAULT_RATES = {
    ("live_30", "foundation"): 20000, ("live_30", "conversation"): 26000, ("live_30", "advanced"): 39000,
    ("live_45", "foundation"): 30000, ("live_45", "conversation"): 39000, ("live_45", "advanced"): 58500,
    ("voice_review", "foundation"): 2500, ("voice_review", "conversation"): 3000, ("voice_review", "advanced"): 4000,
    ("recording_min", "foundation"): 6000, ("recording_min", "conversation"): 6000, ("recording_min", "advanced"): 8000,
}


class PayRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _paid_partner(user_id: str) -> dict | None:
    if not is_ready("paid_partners"):
        return None
    rows = (_t("exchange_partner").select("user_id, roles, status").eq("user_id", user_id).limit(1).execute()).data or []
    if rows and set(rows[0].get("roles") or []) & set(PAID_ROLES):
        return rows[0]
    return None


def rate(work_type: str, band: str, at: datetime) -> tuple[int, str | None]:
    """The rate in force at `at` (doc 05 §2.5: the rate when booked applies)."""
    rows = [r for r in (_t("exchange_rate").select("*").eq("work_type", work_type).eq("band", band).execute()).data or []
            if _ts(r["effective_from"]) <= at]
    if rows:
        r = max(rows, key=lambda x: str(x["effective_from"]))
        return int(r["amount_minor"]), r["id"]
    return DEFAULT_RATES.get((work_type, band), 0), None


def _accrue(partner_id: str, kind: str, amount: int, session_kind: str, ref_id: str, rate_id: str | None,
            now: datetime, note: str | None = None) -> dict | None:
    if amount <= 0:
        return None
    try:
        return (_t("exchange_earning").insert({
            "partner_id": partner_id, "kind": kind, "status": "held", "amount_minor": int(amount), "session_kind": session_kind,
            "ref_id": ref_id, "rate_id": rate_id, "hold_until": (now + HOLD).isoformat(), "note": note}).execute()).data[0]
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return None                    # already accrued for this session
        raise


def _protections_this_month(partner_id: str, now: datetime) -> int:
    since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return sum(1 for r in (_t("exchange_earning").select("kind, created_at").eq("partner_id", partner_id)
               .eq("kind", "no_show_protection").execute()).data or [] if str(r.get("created_at") or "9999") >= since)


def pay_share(present_s: int, length_s: int, joined_late_s: int | None) -> float:
    """Doc 02 §4.2-4.3: full at 90% present and joined by T+5; 75% if joined T+5..T+10;
    prorated at 50-90% present; nothing under 50%."""
    if length_s <= 0:
        return 0.0
    share = present_s / length_s
    if share < 0.5:
        return 0.0
    if share < 0.9:
        return round(share, 2)
    if joined_late_s is not None and joined_late_s > 600:
        return round(share, 2)
    if joined_late_s is not None and joined_late_s > 300:
        return 0.75
    return 1.0


def accrue_pair(s: dict, now: datetime | None = None) -> dict | None:
    """A 1:1 session settled: pay a paid partner for it per doc 02 §4."""
    from app.services.exchange_group_service import _band_am
    from app.services.exchange_presence_service import present_seconds
    now = now or _now()
    if not _paid_partner(s["partner_id"]):
        return None
    start = _ts(s["scheduled_at"])
    length = int(s["length_min"])
    end = start + timedelta(minutes=length)
    band = _band_am(s["learner_id"]) or "foundation"
    amount, rate_id = rate("live_45" if length > 30 else "live_30", band, start)
    if s["status"] == "missed" and s.get("missed_by") == s["learner_id"] and s.get("partner_joined_at") \
            and _ts(s["partner_joined_at"]) <= start + timedelta(minutes=5):
        if _protections_this_month(s["partner_id"], now) >= NO_SHOW_PROTECTION_CAP:
            return None
        return _accrue(s["partner_id"], "no_show_protection", amount // 2, "pair", s["id"], rate_id, now)
    if s["status"] != "completed":
        return None
    secs = present_seconds(s["id"], s["partner_id"], start, end)
    joined = _ts(s["partner_joined_at"]) if s.get("partner_joined_at") else None
    late = int((joined - start).total_seconds()) if joined else None
    share = pay_share(secs, length * 60, late) if secs else 1.0     # no presence data: the session completed
    kind = "live" if share >= 1.0 else "live_partial"
    return _accrue(s["partner_id"], kind, int(amount * share), "pair", s["id"], rate_id, now)


def accrue_group(g: dict, now: datetime | None = None) -> dict | None:
    """A group the paid partner hosted (foundation support, fluent English)."""
    from app.services.exchange_presence_service import present_seconds
    now = now or _now()
    if g["kind"] == "peer_guide" or not _paid_partner(g["host_id"]):
        return None
    start = _ts(g["starts_at"])
    length = int(g["length_min"])
    amount, rate_id = rate("live_45" if length > 30 else "live_30", g["band"], start)
    members = [m for m in (_t("exchange_group_member").select("role, status").eq("group_id", g["id"]).execute()).data or []
               if m["role"] == "member"]
    if not any(m["status"] in ("attended", "partial") for m in members):
        if _protections_this_month(g["host_id"], now) >= NO_SHOW_PROTECTION_CAP:
            return None
        return _accrue(g["host_id"], "no_show_protection", amount // 2, "group", g["id"], rate_id, now)
    secs = present_seconds(g["id"], g["host_id"], start, start + timedelta(minutes=length))
    share = pay_share(secs, length * 60, None) if secs else 1.0
    return _accrue(g["host_id"], "live" if share >= 1.0 else "live_partial", int(amount * share), "group", g["id"], rate_id, now)


def accrue_review(note: dict, now: datetime | None = None) -> dict | None:
    now = now or _now()
    if not note.get("reviewer_id") or not _paid_partner(note["reviewer_id"]):
        return None
    amount, rate_id = rate("voice_review", note.get("band") or "foundation", _ts(note.get("reviewed_at") or now))
    return _accrue(note["reviewer_id"], "voice_review", amount, "voice_note", note["id"], rate_id, now)


# ── the partner's view ──────────────────────────────────────────────────────

def mine(partner_id: str) -> dict:
    rows = (_t("exchange_earning").select("id, kind, status, amount_minor, currency, session_kind, ref_id, hold_until, note, created_at")
            .eq("partner_id", partner_id).execute()).data or []
    dest = (_t("exchange_payout_destination").select("method, msisdn, verified_at, cooldown_until").eq("partner_id", partner_id)
            .limit(1).execute()).data or []
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + int(r["amount_minor"])
    d = dest[0] if dest else None
    if d and d.get("msisdn"):
        d = {**d, "msisdn": f"…{d['msisdn'][-3:]}"}
    return {"enabled": is_ready("paid_partners"), "paid_role": bool(_paid_partner(partner_id)),
            "totals": by, "earnings": sorted(rows, key=lambda r: str(r.get("created_at") or "9999"), reverse=True)[:100],
            "destination": d, "minimum_minor": MIN_PAYOUT_MINOR}


def set_destination(partner_id: str, msisdn: str, registered_name: str, now: datetime | None = None) -> dict:
    """One M-Pesa line in the partner's own name. A change waits 7 days and is re-verified
    by ops (doc 02 §5.4)."""
    now = now or _now()
    if not _paid_partner(partner_id):
        raise PayRefused("not_a_paid_partner")
    digits = "".join(ch for ch in msisdn if ch.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    if not (len(digits) == 12 and digits.startswith("254") and digits[3] in "17"):
        raise PayRefused("bad_number")
    old = (_t("exchange_payout_destination").select("*").eq("partner_id", partner_id).limit(1).execute()).data or []
    row = {"partner_id": partner_id, "method": "mpesa_b2c", "msisdn": digits, "registered_name": registered_name.strip()[:120],
           "verified_at": None, "cooldown_until": (now + COOLDOWN).isoformat() if old else None, "updated_at": now.isoformat()}
    _t("exchange_payout_destination").upsert(row).execute()
    return mine(partner_id)


# ── ops ─────────────────────────────────────────────────────────────────────

def _ops(actor: str) -> None:
    if not hs.admin_can(actor, "ops"):
        raise PayRefused("not_allowed")


def set_rate(actor: str, work_type: str, band: str, amount_minor: int, effective_from: str) -> dict:
    """A new rate from a date (14 days' notice, doc 05 §2.5, is ops' to give)."""
    _ops(actor)
    if (work_type, band) not in DEFAULT_RATES or int(amount_minor) < 0:
        raise PayRefused("bad_value")
    return (_t("exchange_rate").insert({"work_type": work_type, "band": band, "amount_minor": int(amount_minor),
                                        "effective_from": effective_from}).execute()).data[0]


def set_roles(actor: str, partner_id: str, roles: list[str]) -> dict:
    _ops(actor)
    allowed = {"barter", "pap", "fep", "reviewer"}
    if not roles or set(roles) - allowed:
        raise PayRefused("bad_value")
    rows = (_t("exchange_partner").update({"roles": sorted(set(roles))}).eq("user_id", partner_id).execute()).data or []
    if not rows:
        raise PayRefused("no_such_partner")
    return rows[0]


def verify_destination(actor: str, partner_id: str, now: datetime | None = None) -> dict:
    _ops(actor)
    now = now or _now()
    rows = (_t("exchange_payout_destination").update({"verified_at": now.isoformat()}).eq("partner_id", partner_id)
            .execute()).data or []
    if not rows:
        raise PayRefused("no_destination")
    return rows[0]


def release(now: datetime | None = None) -> int:
    """Daily: earnings past their hold, with no open case about the partner, become releasable."""
    now = now or _now()
    open_cases = {r["about_user_id"] for r in (_t("concern_report").select("about_user_id, status").neq("status", "closed")
                  .execute()).data or [] if r.get("about_user_id")}
    n = 0
    for r in (_t("exchange_earning").select("id, partner_id, status, hold_until").eq("status", "held").execute()).data or []:
        if _ts(r["hold_until"]) <= now and r["partner_id"] not in open_cases:
            _t("exchange_earning").update({"status": "releasable"}).eq("id", r["id"]).execute()
            n += 1
    return n


def build_batch(now: datetime | None = None) -> dict | None:
    """Friday: sum releasable earnings per partner; at least KES 500 to a verified line out of
    its cooldown, and the partner not suspended, makes a payout. Smaller sums roll over."""
    from app.services.exchange_case_service import suspended
    now = now or _now()
    if not is_ready("paid_partners"):
        return None
    rows = (_t("exchange_earning").select("*").in_("status", ["releasable"]).execute()).data or []
    reversals = (_t("exchange_earning").select("*").eq("kind", "reversal").eq("status", "held").execute()).data or []
    by: dict[str, list[dict]] = {}
    for r in rows + reversals:
        by.setdefault(r["partner_id"], []).append(r)
    dests = {d["partner_id"]: d for d in (_t("exchange_payout_destination").select("*").execute()).data or []}
    batch = None
    total = 0
    for pid, items in by.items():
        amount = sum(int(i["amount_minor"]) for i in items)
        d = dests.get(pid)
        if amount < MIN_PAYOUT_MINOR or not d or not d.get("verified_at") or \
                (d.get("cooldown_until") and _ts(d["cooldown_until"]) > now) or suspended(pid, now):
            continue
        if batch is None:
            batch = (_t("exchange_payout_batch").insert({"status": "building"}).execute()).data[0]
        payout = (_t("exchange_payout").insert({"batch_id": batch["id"], "partner_id": pid, "amount_minor": amount,
                                                "method": d["method"], "originator_ref": str(uuid.uuid4())})
                  .execute()).data[0]
        for i in items:
            _t("exchange_earning").update({"status": "batched", "payout_id": payout["id"]}).eq("id", i["id"]).execute()
        total += amount
    if batch:
        _t("exchange_payout_batch").update({"total_minor": total}).eq("id", batch["id"]).execute()
        batch["total_minor"] = total
    return batch


def batches(actor: str) -> list[dict]:
    _ops(actor)
    rows = (_t("exchange_payout_batch").select("*").execute()).data or []
    return sorted(rows, key=lambda r: str(r.get("created_at") or "9999"), reverse=True)


def approve(actor: str, batch_id: str) -> dict:
    """Two different staff approve a batch before it is sent (doc 06 §16)."""
    _ops(actor)
    rows = (_t("exchange_payout_batch").select("*").eq("id", batch_id).limit(1).execute()).data or []
    if not rows or rows[0]["status"] not in ("building", "approved"):
        raise PayRefused("not_open")
    who = sorted(set((rows[0].get("approved_by") or []) + [actor]))
    status = "approved" if len(who) >= 2 else "building"
    return (_t("exchange_payout_batch").update({"approved_by": who, "status": status}).eq("id", batch_id).execute()).data[0]


async def send(actor: str, batch_id: str) -> dict:
    """Hand each payout to its provider. None is configured yet (see the module note)."""
    _ops(actor)
    rows = (_t("exchange_payout_batch").select("*").eq("id", batch_id).limit(1).execute()).data or []
    if not rows or rows[0]["status"] != "approved":
        raise PayRefused("not_approved")
    sent = failed = 0
    for p in (_t("exchange_payout").select("*").eq("batch_id", batch_id).eq("status", "pending").execute()).data or []:
        _t("exchange_payout").update({"status": "failed", "attempts": int(p["attempts"]) + 1,
                                      "last_error": "not_configured"}).eq("id", p["id"]).execute()
        _t("exchange_earning").update({"status": "releasable", "payout_id": None}).eq("payout_id", p["id"]).execute()
        failed += 1
    _t("exchange_payout_batch").update({"status": "sent"}).eq("id", batch_id).execute()
    return {"sent": sent, "failed": failed, "reason": "no payout provider configured" if failed else None}
