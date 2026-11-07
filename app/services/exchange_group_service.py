"""Exchange groups: opt-in sessions beside 1:1 (§159; exchange-extension-plan XE-16, XE-17,
XE-2; exchange doc 01 §5-§6).

1:1 stays the default. A host opens a group (one-off, or a weekly series of 4-8 weeks in the
host's zone) and up to 3 people book a seat:

  foundation_support  an Amharic partner (English B1+) with 2-3 Foundation learners,
                      Phase 0 learners and exchange guests included (XE-22, XE-23)
  peer_guide          a learner ops granted 'peer_guide' (exchange_host_grant) with 2-3
                      learners 1-2 bands below. No pronunciation sign-off
  fluent_english      a fluent-English partner ('fep') with Exchange+ members (XE-25)

Hard rules (doc 01 §6.1), checked for every seat against everyone already in the group:
blocks and household links both ways, an active suspension, the group's band, the lone-woman
rule (a woman is never the only woman unless she chose "any mix"), a same-gender-only
preference, and age bands (never 18-25 with 41+ unless everyone widened). Ages need a date of
birth held privately (exchange_sensitive, XE-17): someone without one can't take a seat.
A database rule stops one person holding two overlapping seats.

Behind `exchange_groups`.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.flags import is_ready
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

KINDS = ("foundation_support", "peer_guide", "fluent_english")
BANDS = ("foundation", "conversation", "advanced")
LENGTHS = (30, 45)
FREE_CANCEL = timedelta(hours=2)          # doc 01 §9.4
AGE_BANDS = ((18, 25), (26, 40), (41, 200))
GROUP_ALLOWANCE = {"free_tier": 1, "learner_tier": 2, "tutor_tier": 4, "intensive_tier": 8}   # a month
ATTENDED, PARTIAL = 0.80, 0.25            # doc 01 §9.1
DST_AHEAD = timedelta(days=8)


class GroupRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _t(name: str):
    return db.schema("app").table(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _on() -> None:
    from app.services.exchange_service import enabled
    if not enabled() or not is_ready("exchange_groups"):
        raise GroupRefused("not_enabled")


# ── who may host, who may join ──────────────────────────────────────────────

def _partner(user_id: str) -> dict | None:
    rows = (_t("exchange_partner").select("*").eq("user_id", user_id).limit(1).execute()).data or []
    return rows[0] if rows else None


def _grants(user_id: str) -> set[str]:
    return {r["role"] for r in (_t("exchange_host_grant").select("role").eq("user_id", user_id).execute()).data or []}


def may_host(user_id: str, kind: str) -> bool:
    p = _partner(user_id)
    if kind == "foundation_support":
        level = (p or {}).get("english_level") or (p or {}).get("english_self")
        return bool(p and p["status"] == "active" and level in ("B1", "B2", "C1"))
    if kind == "fluent_english":
        return bool(p and p["status"] == "active" and "fep" in (p.get("roles") or []))
    return "peer_guide" in _grants(user_id) or "alumni_host" in _grants(user_id)


def _band_am(user_id: str) -> str | None:
    rows = (_t("exchange_language_level").select("band").eq("user_id", user_id).eq("language", "am").limit(1)
            .execute()).data or []
    if rows:
        return rows[0]["band"]
    from app.services import exchange_service as ex
    if ex.foundation_only(user_id):
        return "foundation"
    lesson = ex._lesson(user_id)
    return "foundation" if lesson <= 20 else "conversation" if lesson <= 60 else "advanced"


def _may_join(user_id: str, g: dict) -> None:
    from app.services import exchange_service as ex
    if g["kind"] == "fluent_english":
        p = _partner(user_id)
        if not p or p["status"] != "active" or not p.get("plus_until") or _ts(p["plus_until"]) < _now():
            raise GroupRefused("exchange_plus_only")
        return
    if not ex.eligible(user_id):
        raise GroupRefused("not_eligible")
    band = _band_am(user_id)
    if g["kind"] == "foundation_support" and band != "foundation":
        raise GroupRefused("wrong_band")
    if g["kind"] == "peer_guide":
        host = _band_am(g["host_id"]) or "conversation"
        gap = BANDS.index(host) - BANDS.index(band or "foundation")
        if gap < 1:
            raise GroupRefused("wrong_band")


# ── XE-17: age and private details ──────────────────────────────────────────

def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def age_band(age: int) -> int:
    return next(i for i, (lo, hi) in enumerate(AGE_BANDS) if lo <= age <= hi)


def set_details(user_id: str, date_of_birth: str | None, widen_age_band: bool | None = None,
                pod_any_mix: bool | None = None, now: datetime | None = None) -> dict:
    """A date of birth (kept where no client can read it) and the two matching choices."""
    now = now or _now()
    if date_of_birth:
        try:
            dob = date.fromisoformat(date_of_birth)
        except ValueError as exc:
            raise GroupRefused("bad_value") from exc
        if age_on(dob, now.date()) < 18:
            raise GroupRefused("under_18")
        if age_on(dob, now.date()) > 120:
            raise GroupRefused("bad_value")
        _t("exchange_sensitive").upsert({"user_id": user_id, "date_of_birth": dob.isoformat(),
                                         "dob_method": "declared", "updated_at": now.isoformat()}).execute()
    pref = {"user_id": user_id, "updated_at": now.isoformat()}
    if widen_age_band is not None:
        pref["widen_age_band"] = bool(widen_age_band)
    if pod_any_mix is not None:
        pref["pod_any_mix"] = bool(pod_any_mix)
    if len(pref) > 2:
        _t("exchange_match_pref").upsert(pref).execute()
    return details(user_id, now)


def details(user_id: str, now: datetime | None = None) -> dict:
    """What the person sees of their own details: never the date itself back, only whether
    it's on file and the age band it puts them in."""
    now = now or _now()
    s = (_t("exchange_sensitive").select("date_of_birth").eq("user_id", user_id).limit(1).execute()).data or []
    p = (_t("exchange_match_pref").select("*").eq("user_id", user_id).limit(1).execute()).data or []
    band = None
    if s:
        lo, hi = AGE_BANDS[age_band(age_on(date.fromisoformat(str(s[0]["date_of_birth"])), now.date()))]
        band = f"{lo}-{hi}" if hi < 200 else f"{lo}+"
    pref = p[0] if p else {}
    return {"has_birth_date": bool(s), "age_band": band, "widen_age_band": bool(pref.get("widen_age_band")),
            "pod_any_mix": bool(pref.get("pod_any_mix"))}


def _people(ids: list[str], now: datetime) -> dict[str, dict]:
    """Gender (gender_preference), age band and preferences for the pod rules."""
    from app.services.exchange_service import _genders
    g = _genders(ids)
    dob = {r["user_id"]: r["date_of_birth"] for r in (_t("exchange_sensitive").select("user_id, date_of_birth")
           .in_("user_id", ids).execute()).data or []} if ids else {}
    pref = {r["user_id"]: r for r in (_t("exchange_match_pref").select("*").in_("user_id", ids).execute()).data or []} if ids else {}
    out = {}
    for u in ids:
        d = dob.get(u)
        out[u] = {"gender": (g.get(u) or {}).get("gender"), "same_pod": (g.get(u) or {}).get("same_gender_pod"),
                  "age_band": age_band(age_on(date.fromisoformat(str(d)), now.date())) if d else None,
                  "any_mix": bool((pref.get(u) or {}).get("pod_any_mix")),
                  "widen": bool((pref.get(u) or {}).get("widen_age_band"))}
    return out


def pod_ok(people: dict[str, dict]) -> str | None:
    """None if this set of people may share a group, else the rule it breaks (doc 01 §6.1)."""
    for u, x in people.items():
        if x["age_band"] is None:
            return "needs_birth_date"
        if x.get("same_pod") == "requirement" and x["gender"]:
            if any(y["gender"] != x["gender"] for v, y in people.items() if v != u):
                return "same_gender_only"
    women = [u for u, x in people.items() if x["gender"] == "woman"]
    if len(women) == 1 and len(people) > 1 and not people[women[0]]["any_mix"]:
        return "lone_woman"
    bands = {x["age_band"] for x in people.values()}
    if 0 in bands and 2 in bands and not all(x["widen"] for x in people.values()):
        return "age_span"
    return None


# ── creating, listing, booking ──────────────────────────────────────────────

def _row(g: dict, seats: list[dict], user_id: str | None = None) -> dict:
    booked = [m for m in seats if m["status"] == "booked" and m["role"] == "member"]
    return {"id": g["id"], "kind": g["kind"], "starts_at": g["starts_at"], "length_min": g["length_min"],
            "band": g["band"], "status": g["status"], "seats_left": max(0, int(g["capacity"]) - len(booked)),
            "series_id": g.get("series_id"), "mine": any(m["user_id"] == user_id and m["status"] == "booked" for m in seats),
            "hosting": g["host_id"] == user_id}


def create(host_id: str, kind: str, starts_at: str, length_min: int, band: str, capacity: int = 3,
           weeks: int | None = None, now: datetime | None = None) -> list[dict]:
    """One group, or a weekly series of `weeks` (4-8) at the same wall-clock time in the host's zone."""
    _on()
    now = now or _now()
    if kind not in KINDS or length_min not in LENGTHS or band not in BANDS or not 2 <= int(capacity) <= 3:
        raise GroupRefused("bad_value")
    if kind == "foundation_support" and band != "foundation":
        raise GroupRefused("bad_value")
    if not may_host(host_id, kind):
        raise GroupRefused("not_a_host")
    from app.services.exchange_case_service import suspended
    if suspended(host_id, now):
        raise GroupRefused("suspended")
    at = _ts(starts_at)
    if at <= now + timedelta(hours=2) or at - now > timedelta(days=14):
        raise GroupRefused("bad_time")
    series_id = None
    starts = [at]
    if weeks:
        if not 4 <= int(weeks) <= 8:
            raise GroupRefused("bad_value")
        p = _partner(host_id) or {}
        tz = p.get("timezone") or "Africa/Addis_Ababa"
        local = at.astimezone(ZoneInfo(tz))
        series_id = (_t("exchange_series").insert({
            "kind": kind, "host_id": host_id, "anchor_tz": tz, "day_of_week": local.weekday(),
            "local_time": local.time().replace(second=0, microsecond=0).isoformat(), "length_min": length_min,
            "band": band, "weeks": int(weeks), "starts_on": local.date().isoformat()}).execute()).data[0]["id"]
        starts = [datetime.combine(local.date() + timedelta(weeks=i), local.time(), tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
                  for i in range(int(weeks))]
    made = []
    for s in starts:
        g = (_t("exchange_group").insert({"kind": kind, "starts_at": s.isoformat(), "length_min": length_min, "band": band,
                                          "host_id": host_id, "capacity": int(capacity), "series_id": series_id})
             .execute()).data[0]
        end = s + timedelta(minutes=length_min)
        try:
            _t("exchange_group_member").insert({"group_id": g["id"], "user_id": host_id, "role": "host",
                                                "slot": f"[{s.isoformat()},{end.isoformat()})"}).execute()
        except Exception as exc:  # noqa: BLE001
            _t("exchange_group").delete().eq("id", g["id"]).execute()
            if "exclu" in str(exc).lower() or "overlap" in str(exc).lower() or "conflicting" in str(exc).lower():
                raise GroupRefused("overlap") from exc
            raise
        made.append(_row(g, [{"user_id": host_id, "role": "host", "status": "booked"}], host_id))
    return made


def _seats(group_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not group_ids:
        return out
    for m in (_t("exchange_group_member").select("*").in_("group_id", group_ids).execute()).data or []:
        out.setdefault(m["group_id"], []).append(m)
    return out


def open_groups(user_id: str, now: datetime | None = None) -> list[dict]:
    """Groups in the next 14 days with a seat this person could take, and their own."""
    _on()
    now = now or _now()
    rows = (_t("exchange_group").select("*").eq("status", "scheduled").gte("starts_at", now.isoformat())
            .lte("starts_at", (now + timedelta(days=14)).isoformat()).execute()).data or []
    seats = _seats([g["id"] for g in rows])
    out = []
    for g in sorted(rows, key=lambda r: str(r["starts_at"])):
        r = _row(g, seats.get(g["id"], []), user_id)
        if r["mine"] or r["hosting"]:
            out.append(r)
            continue
        if r["seats_left"] <= 0:
            continue
        try:
            _check_seat(user_id, g, seats.get(g["id"], []), now)
        except GroupRefused as e:
            if e.reason != "needs_birth_date":
                continue
            r["needs_birth_date"] = True       # shown, with "add your date of birth to book"
        out.append(r)
    return out


def _check_seat(user_id: str, g: dict, seats: list[dict], now: datetime) -> None:
    from app.services.exchange_case_service import suspended
    from app.services.exchange_service import _blocked_pairs
    if suspended(user_id, now):
        raise GroupRefused("suspended")
    _may_join(user_id, g)
    others = [m["user_id"] for m in seats if m["status"] == "booked"]
    if user_id in others:
        raise GroupRefused("already_booked")
    if set(others) & _blocked_pairs(user_id):
        raise GroupRefused("unavailable")           # never says why (doc 01 E23)
    why = pod_ok(_people(others + [user_id], now))
    if why:
        raise GroupRefused("needs_birth_date" if why == "needs_birth_date" else "unavailable")


def _group_allowance_left(user_id: str, now: datetime) -> int:
    from app.services.exchange_service import _tier
    limit = GROUP_ALLOWANCE.get(_tier(user_id), 0)
    since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    mine = [m for m in (_t("exchange_group_member").select("id, status, role, created_at").eq("user_id", user_id)
            .eq("role", "member").execute()).data or [] if str(m.get("created_at") or "9999") >= since
            and m["status"] in ("booked", "attended", "partial", "no_show")]
    return max(0, limit - len(mine))


def book(user_id: str, group_id: str, now: datetime | None = None) -> dict:
    _on()
    now = now or _now()
    rows = (_t("exchange_group").select("*").eq("id", group_id).limit(1).execute()).data or []
    if not rows or rows[0]["status"] != "scheduled" or _ts(rows[0]["starts_at"]) <= now:
        raise GroupRefused("not_open")
    g = rows[0]
    seats = _seats([group_id]).get(group_id, [])
    if len([m for m in seats if m["status"] == "booked" and m["role"] == "member"]) >= int(g["capacity"]):
        raise GroupRefused("full")
    _check_seat(user_id, g, seats, now)
    if g["kind"] != "fluent_english" and _group_allowance_left(user_id, now) <= 0:
        raise GroupRefused("allowance_used")
    start = _ts(g["starts_at"])
    end = start + timedelta(minutes=int(g["length_min"]))
    existing = [m for m in seats if m["user_id"] == user_id]
    try:
        if existing:
            _t("exchange_group_member").update({"status": "booked", "late_cancel": False}).eq("id", existing[0]["id"]).execute()
        else:
            _t("exchange_group_member").insert({"group_id": group_id, "user_id": user_id, "role": "member",
                                                "slot": f"[{start.isoformat()},{end.isoformat()})"}).execute()
    except Exception as exc:  # noqa: BLE001
        if "exclu" in str(exc).lower() or "overlap" in str(exc).lower() or "conflicting" in str(exc).lower():
            raise GroupRefused("overlap") from exc
        raise
    return _row(g, _seats([group_id]).get(group_id, []), user_id)


def leave(user_id: str, group_id: str, now: datetime | None = None) -> dict:
    """Free at least 2 hours before (doc 01 §9.4), otherwise a late cancel. A host leaving
    cancels the group for everyone."""
    _on()
    now = now or _now()
    rows = (_t("exchange_group").select("*").eq("id", group_id).limit(1).execute()).data or []
    if not rows:
        raise GroupRefused("not_open")
    g = rows[0]
    late = _ts(g["starts_at"]) - now < FREE_CANCEL
    if g["host_id"] == user_id:
        _t("exchange_group").update({"status": "cancelled", "cancel_reason": "host"}).eq("id", group_id).execute()
        _t("exchange_group_member").update({"status": "cancelled"}).eq("group_id", group_id).eq("status", "booked").execute()
        return {"status": "cancelled", "late": late}
    got = (_t("exchange_group_member").update({"status": "cancelled", "late_cancel": late}).eq("group_id", group_id)
           .eq("user_id", user_id).eq("status", "booked").execute()).data or []
    if not got:
        raise GroupRefused("not_booked")
    return {"status": "cancelled", "late": late}


def mine(user_id: str, now: datetime | None = None) -> list[dict]:
    now = now or _now()
    ids = [m["group_id"] for m in (_t("exchange_group_member").select("group_id, status").eq("user_id", user_id)
           .eq("status", "booked").execute()).data or []]
    if not ids:
        return []
    rows = (_t("exchange_group").select("*").in_("id", ids).execute()).data or []
    seats = _seats(ids)
    return [_row(g, seats.get(g["id"], []), user_id) for g in sorted(rows, key=lambda r: str(r["starts_at"]))
            if _ts(g["starts_at"]) + timedelta(minutes=int(g["length_min"])) > now]


# ── the call ────────────────────────────────────────────────────────────────

async def join(user_id: str, group_id: str, now: datetime | None = None) -> dict:
    """From 5 minutes before (doc 01 §9.2) until half the session has gone."""
    from app.services import calling_service as calls
    _on()
    now = now or _now()
    rows = (_t("exchange_group").select("*").eq("id", group_id).limit(1).execute()).data or []
    if not rows:
        raise GroupRefused("not_open")
    g = rows[0]
    seat = [m for m in _seats([group_id]).get(group_id, []) if m["user_id"] == user_id and m["status"] == "booked"]
    if not seat:
        raise GroupRefused("not_booked")
    start = _ts(g["starts_at"])
    if not (start - timedelta(minutes=5) <= now <= start + timedelta(minutes=int(g["length_min"]) // 2)):
        raise GroupRefused("not_now")
    end = start + timedelta(minutes=int(g["length_min"]) + 2)
    url = g.get("room_url")
    if not url:
        url = await calls.create_room(f"exg-{g['id'].replace('-', '')}", int(end.timestamp()) + 600,
                                      properties={"start_video_off": True, "privacy": "private", "enable_screenshare": False})
        if not url:
            raise GroupRefused("no_room")
        _t("exchange_group").update({"room_url": url}).eq("id", group_id).execute()
    from app.services.exchange_service import _first_name, _profiles
    name = _first_name(_profiles([user_id]).get(user_id))
    if calls.is_livekit_room(url):
        access = calls.livekit_access(url, user_id, name, int(end.timestamp()), start_video_off=True)
        if not access:
            raise GroupRefused("no_room")
        return {"livekit": access, "room_url": url, "kind": g["kind"], "length_min": g["length_min"],
                "starts_at": g["starts_at"], "host": g["host_id"] == user_id}
    token = await calls.create_daily_meeting_token(url.rstrip("/").rsplit("/", 1)[-1], user_id, name, int(end.timestamp()))
    if not token:
        raise GroupRefused("no_room")
    return {"livekit": None, "room_url": f"{url}{'&' if '?' in url else '?'}t={token}", "kind": g["kind"],
            "length_min": g["length_min"], "starts_at": g["starts_at"], "host": g["host_id"] == user_id}


# ── the clock (XE-9) ────────────────────────────────────────────────────────

async def tick(now: datetime | None = None) -> int:
    """scheduled -> live at the start; live -> completed 5 minutes after the end, with each
    seat's attendance from the provider's presence (XE-10) or, without it, from joining."""
    from app.services.exchange_presence_service import present_seconds
    now = now or _now()
    if not is_ready("exchange_groups"):
        return 0
    n = 0
    for g in (_t("exchange_group").select("*").in_("status", ["scheduled", "live"])
              .lte("starts_at", now.isoformat()).execute()).data or []:
        start = _ts(g["starts_at"])
        end = start + timedelta(minutes=int(g["length_min"]))
        if g["status"] == "scheduled":
            _t("exchange_group").update({"status": "live"}).eq("id", g["id"]).execute()
            n += 1
        if now < end + timedelta(minutes=5):
            continue
        for m in _seats([g["id"]]).get(g["id"], []):
            if m["status"] != "booked":
                continue
            secs = present_seconds(g["id"], m["user_id"], start, end)
            share = secs / ((end - start).total_seconds() or 1)
            if secs:
                status = "attended" if share >= ATTENDED else "partial" if share >= PARTIAL else "no_show"
            else:
                status = "attended" if m.get("joined_at") else "no_show"
            _t("exchange_group_member").update({"status": status}).eq("id", m["id"]).execute()
        _t("exchange_group").update({"status": "completed"}).eq("id", g["id"]).execute()
        try:
            from app.services import exchange_pay_service as pay
            pay.accrue_group(g, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("group pay for %s failed: %s", g["id"], exc)
        n += 1
    return n


# ── XE-2: clock-change warnings for weekly series ───────────────────────────

async def dst_warnings(now: datetime | None = None) -> int:
    """A series sits at a fixed time in the host's zone; when the clocks change for a
    member (or the host) in the next 8 days, their local time moves. Tell them once."""
    from app.services.dst_service import first_change
    from app.services.notify_service import notify
    now = now or _now()
    if not is_ready("exchange_groups"):
        return 0
    sent = 0
    for s in (_t("exchange_series").select("*").eq("status", "active").execute()).data or []:
        groups = [g for g in (_t("exchange_group").select("id, starts_at, status").eq("series_id", s["id"])
                  .eq("status", "scheduled").execute()).data or [] if _ts(g["starts_at"]) >= now]
        starts = [_ts(g["starts_at"]) for g in groups]
        if len(starts) < 2:
            continue
        people = {m["user_id"] for m in (_t("exchange_group_member").select("user_id, status, group_id")
                  .in_("group_id", [g["id"] for g in groups]).eq("status", "booked").execute()).data or []}
        zones = {r["id"]: r.get("timezone") for r in (_t("user_profiles").select("id, timezone")
                 .in_("id", list(people)).execute()).data or []} if people else {}
        for uid in people:
            tz = zones.get(uid) or ((_partner(uid) or {}).get("timezone")) or "UTC"
            hit = first_change(starts, tz)
            if not hit or hit[0] - now > DST_AHEAD:
                continue
            at, old, new = hit
            body = f"From {at.astimezone(ZoneInfo(tz)):%a %d %b} your weekly group is at {new} your time (was {old}): the clocks change."
            if await notify(uid, "exchange_dst", f"{s['id']}:{at.date()}", "Your group's time moves", body, "/exchange"):
                sent += 1
    return sent
