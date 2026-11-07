"""Presence from the call provider, the every-minute clock, the minute budget and the
mid-call provider switch (§159; exchange-extension-plan XE-9, XE-10, XE-15, XE-18).

XE-10  LiveKit and Daily tell us who joined and left each room (webhooks, signature
       checked). Each event is stored once (`exchange_provider_event`, keyed by the
       provider's event id) and folded into `exchange_presence` intervals. Presence is a
       session record, not activity telemetry (D-3): it decides nothing on its own. Today
       it fills in `partner_joined_at` / `learner_joined_at` when someone connected
       without the join call reaching us, and it is the evidence attached to disputes.
XE-9   `GET /tasks/minute` (pg_cron + pg_net, or the hourly job as a fallback): rooms 15
       minutes ahead, "free to chat" switches that ran out, group sessions' status and
       attendance, presence folding.
XE-15  Participant-minutes per month and pool, recomputed (idempotent) and alerted at 80%
       and 95%. It never refuses a booking.
XE-18  Two people reporting a LiveKit fault within 60 seconds move the session to a Daily
       room; the next join hands out Daily.
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

MERGE_GAP = timedelta(seconds=60)
FAULT_WINDOW = timedelta(seconds=60)
ROOM_AHEAD = timedelta(minutes=15)


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ── rooms and sessions ──────────────────────────────────────────────────────

def session_for_room(room: str | None) -> tuple[str, str] | None:
    """('pair' | 'group', session id) from a room name: ex-<hex> or exg-<hex>."""
    name = (room or "").rstrip("/").rsplit("/", 1)[-1].replace("livekit://", "")
    for prefix, kind in (("exg-", "group"), ("ex-", "pair")):
        if name.startswith(prefix):
            try:
                return kind, str(UUID(name[len(prefix):]))
            except ValueError:
                return None
    return None


# ── webhooks ────────────────────────────────────────────────────────────────

def verify_livekit(body: bytes, auth: str | None) -> bool:
    """LiveKit signs a webhook with a JWT (the API key and secret) whose `sha256` claim is
    the base64 SHA-256 of the body."""
    if not auth or not settings.LIVEKIT_SECRET:
        return False
    try:
        claims = jwt.decode(auth.removeprefix("Bearer ").strip(), settings.LIVEKIT_SECRET, algorithms=["HS256"],
                            options={"verify_aud": False})
    except jwt.PyJWTError:
        return False
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    return claims.get("iss") == settings.LIVEKIT_API_KEY and hmac.compare_digest(str(claims.get("sha256", "")), digest)


def verify_daily(body: bytes, signature: str | None, stamp: str | None) -> bool:
    """Daily: base64 HMAC-SHA256 of "<timestamp>.<body>" with the base64-decoded secret."""
    if not signature or not stamp or not settings.daily_webhook_secret:
        return False
    try:
        key = base64.b64decode(settings.daily_webhook_secret)
    except Exception:  # noqa: BLE001
        return False
    mac = base64.b64encode(hmac.new(key, f"{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(mac, signature)


def _uuid(v) -> str | None:
    try:
        return str(UUID(str(v)))
    except (ValueError, TypeError):
        return None


def normalise(provider: str, event: dict) -> dict | None:
    """One stored event: joined | left | room_finished, with room, user and time."""
    if provider == "livekit":
        kind = {"participant_joined": "joined", "participant_left": "left", "room_finished": "room_finished"}.get(event.get("event"))
        if not kind:
            return None
        at = datetime.fromtimestamp(int(event.get("createdAt") or 0), timezone.utc) if event.get("createdAt") else _now()
        return {"event_id": str(event.get("id") or f"{event.get('event')}-{event.get('createdAt')}"), "event_type": kind,
                "room": (event.get("room") or {}).get("name"),
                "user_id": _uuid((event.get("participant") or {}).get("identity")), "occurred_at": at}
    kind = {"participant.joined": "joined", "participant.left": "left", "meeting.ended": "room_finished"}.get(event.get("type"))
    if not kind:
        return None
    p = event.get("payload") or {}
    stamp = p.get("joined_at") if kind == "joined" else None
    at = datetime.fromtimestamp(float(stamp), timezone.utc) if stamp else (
        datetime.fromtimestamp(float(event["event_ts"]), timezone.utc) if event.get("event_ts") else _now())
    if kind == "left" and p.get("joined_at") and p.get("duration") is not None:
        at = datetime.fromtimestamp(float(p["joined_at"]) + float(p["duration"]), timezone.utc)
    return {"event_id": str(event.get("id") or f"{event.get('type')}-{event.get('event_ts')}"), "event_type": kind,
            "room": p.get("room") or p.get("room_name"), "user_id": _uuid(p.get("user_id")), "occurred_at": at}


def record(provider: str, event: dict) -> bool:
    """Store the event once and fold it into presence. A repeat is ignored."""
    e = normalise(provider, event)
    if not e:
        return False
    try:
        _t("exchange_provider_event").insert({"provider": provider, **e, "occurred_at": e["occurred_at"].isoformat(),
                                              "payload": json.loads(json.dumps(event, default=str))}).execute()
    except Exception as exc:  # noqa: BLE001
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return False
        raise
    fold(provider, e)
    _t("exchange_provider_event").update({"processed_at": _now().isoformat()}) \
        .eq("provider", provider).eq("event_id", e["event_id"]).execute()
    return True


def fold(provider: str, e: dict) -> None:
    hit = session_for_room(e.get("room"))
    if not hit or (e["event_type"] != "room_finished" and not e.get("user_id")):
        return
    kind, sid = hit
    at = e["occurred_at"]
    if e["event_type"] == "joined":
        open_ = [r for r in (_t("exchange_presence").select("id, left_at, joined_at").eq("session_id", sid)
                 .eq("user_id", e["user_id"]).execute()).data or [] if not r.get("left_at")]
        if not open_:
            _t("exchange_presence").insert({"session_kind": kind, "session_id": sid, "user_id": e["user_id"],
                                            "provider": provider, "joined_at": at.isoformat()}).execute()
        if kind == "pair":
            _mark_pair_joined(sid, e["user_id"], at)
        else:
            _t("exchange_group_member").update({"joined_at": at.isoformat()}).eq("group_id", sid) \
                .eq("user_id", e["user_id"]).is_("joined_at", "null").execute()
    elif e["event_type"] == "left":
        for r in (_t("exchange_presence").select("id, left_at").eq("session_id", sid).eq("user_id", e["user_id"])
                  .execute()).data or []:
            if not r.get("left_at"):
                _t("exchange_presence").update({"left_at": at.isoformat()}).eq("id", r["id"]).execute()
    else:
        for r in (_t("exchange_presence").select("id, left_at").eq("session_id", sid).execute()).data or []:
            if not r.get("left_at"):
                _t("exchange_presence").update({"left_at": at.isoformat()}).eq("id", r["id"]).execute()


def _mark_pair_joined(sid: str, user_id: str, at: datetime) -> None:
    """The provider saw them connect: the no-show check and the hourly settle use it."""
    rows = (_t("exchange_session").select("id, learner_id, partner_id, learner_joined_at, partner_joined_at")
            .eq("id", sid).limit(1).execute()).data or []
    if not rows:
        return
    s = rows[0]
    col = "learner_joined_at" if user_id == s["learner_id"] else "partner_joined_at" if user_id == s["partner_id"] else None
    if col and not s.get(col):
        _t("exchange_session").update({col: at.isoformat()}).eq("id", sid).execute()


def present_seconds(session_id: str, user_id: str, start: datetime, end: datetime) -> int:
    """Time connected inside [start, end], gaps of a minute or less merged (doc 06 §8)."""
    rows = sorted(((_ts(r["joined_at"]), _ts(r["left_at"]) if r.get("left_at") else end)
                   for r in (_t("exchange_presence").select("joined_at, left_at").eq("session_id", session_id)
                             .eq("user_id", user_id).execute()).data or []), key=lambda x: x[0])
    merged: list[list[datetime]] = []
    for a, b in rows:
        if merged and a - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    total = 0.0
    for a, b in merged:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


# ── XE-18: provider fault → Daily ───────────────────────────────────────────

async def provider_fault(user_id: str, session_id: str, now: datetime | None = None) -> dict:
    """A client couldn't connect to LiveKit. Two people within 60 s move the call to Daily."""
    from app.services.calling_service import create_daily_room, is_livekit_room
    now = now or _now()
    rows = (_t("exchange_session").select("*").eq("id", session_id).limit(1).execute()).data or []
    if not rows or user_id not in (rows[0]["learner_id"], rows[0]["partner_id"]):
        return {"switched": False}
    s = rows[0]
    if not is_livekit_room(s.get("room_url")):
        return {"switched": False, "provider": "daily"}
    try:
        _t("exchange_provider_event").insert({
            "provider": "livekit", "event_id": f"fault-{session_id}-{user_id}-{int(now.timestamp()) // 60}",
            "event_type": "client_fault", "room": s.get("room_url"), "user_id": user_id,
            "occurred_at": now.isoformat(), "payload": {}}).execute()
    except Exception:  # noqa: BLE001
        pass
    since = (now - FAULT_WINDOW).isoformat()
    who = {r["user_id"] for r in (_t("exchange_provider_event").select("user_id, occurred_at, event_type, room")
           .eq("event_type", "client_fault").eq("room", s.get("room_url")).execute()).data or []
           if str(r["occurred_at"]) >= since}
    if len(who) < 2 or not settings.DAILY_API_KEY:
        return {"switched": False, "provider": "livekit"}
    end = _ts(s["scheduled_at"]) + timedelta(minutes=int(s["length_min"]) + 30)
    url = await create_daily_room(f"ex-{s['id'].replace('-', '')}", int(end.timestamp()),
                                  properties={"start_video_off": True, "privacy": "private", "enable_screenshare": False})
    if not url:
        return {"switched": False, "provider": "livekit"}
    _t("exchange_session").update({"room_url": url}).eq("id", session_id).execute()
    return {"switched": True, "provider": "daily"}


# ── XE-15: the minute budget ────────────────────────────────────────────────

def _pool_for_pair(s: dict) -> str:
    return "taster" if int(s.get("length_min") or 0) == 15 else "barter"


async def minute_budget(now: datetime | None = None) -> dict:
    """Recompute this month's participant-minutes per pool from what happened, and alert
    ops at 80% and 95% once each. Measuring only (XE-15)."""
    from app.services.exchange_service import config
    now = now or _now()
    period = now.date().replace(day=1)
    since = datetime(period.year, period.month, 1, tzinfo=timezone.utc).isoformat()
    used = {"required": 0, "barter": 0, "taster": 0}
    for s in (_t("exchange_session").select("id, status, length_min, scheduled_at").gte("scheduled_at", since)
              .eq("status", "completed").execute()).data or []:
        used[_pool_for_pair(s)] += 2 * int(s["length_min"])
    for g in (_t("exchange_group").select("id, kind, length_min, status, starts_at").gte("starts_at", since)
              .eq("status", "completed").execute()).data or []:
        n = len([m for m in (_t("exchange_group_member").select("status").eq("group_id", g["id"]).execute()).data or []
                 if m["status"] in ("attended", "partial")])
        used["barter" if g["kind"] == "peer_guide" else "required"] += n * int(g["length_min"])
    caps = config("exchange.minute_caps") or {}
    alerts = []
    for pool, pm in used.items():
        cap = int(caps.get(pool) or 1)
        rows = (_t("exchange_minute_pool").select("*").eq("period", period.isoformat()).eq("pool", pool).limit(1)
                .execute()).data or []
        row = rows[0] if rows else {}
        patch = {"period": period.isoformat(), "pool": pool, "cap_pm": cap, "used_pm": pm}
        share = pm / cap
        if share >= 0.95 and not row.get("alerted_95"):
            patch["alerted_95"] = now.isoformat()
            alerts.append((pool, 95))
        elif share >= 0.8 and not row.get("alerted_80"):
            patch["alerted_80"] = now.isoformat()
            alerts.append((pool, 80))
        _t("exchange_minute_pool").upsert(patch).execute()
    if alerts:
        try:
            from app.services import email_service
            if settings.admin_email:
                await email_service.send_email(
                    [settings.admin_email], "Exchange minute budget",
                    "<p>" + "; ".join(f"{p}: {n}% of this month's minutes used" for p, n in alerts) + "</p>",
                    tags=["exchange", "budget"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("budget alert failed: %s", exc)
    return {"used": used, "alerts": len(alerts)}


# ── XE-9: the every-minute clock ────────────────────────────────────────────

async def minute(now: datetime | None = None) -> dict:
    """Idempotent; safe to run every minute, or hourly as a fallback."""
    from app.services import exchange_group_service as groups
    from app.services import exchange_service as ex
    now = now or _now()
    out = {"rooms": 0, "free_expired": 0, "groups": 0}
    if not ex.enabled():
        return {"enabled": False}
    soon = (now + ROOM_AHEAD).isoformat()
    for s in (_t("exchange_session").select("*").eq("status", "booked").lte("scheduled_at", soon)
              .gte("scheduled_at", (now - timedelta(minutes=30)).isoformat()).execute()).data or []:
        if not s.get("room_url"):
            try:
                if await ex._room(s):
                    out["rooms"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("exchange room for %s failed: %s", s["id"], exc)
    for q in (_t("exchange_queue").select("user_id, free_until, session_id").execute()).data or []:
        if q.get("free_until") and not q.get("session_id") and _ts(q["free_until"]) <= now:
            _t("exchange_queue").delete().eq("user_id", q["user_id"]).execute()
            out["free_expired"] += 1
    try:
        out["groups"] = await groups.tick(now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("group tick failed: %s", exc)
    return out
