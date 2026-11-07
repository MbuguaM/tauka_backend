"""Exchange extension (§159, exchange-extension-plan.md).

Pinned:
XE-1   the Ethiopian clock's test vectors (shared with the web and the app).
XE-3   Kenyan/Ethiopian numbers masked; spelled-out numbers flagged, not masked.
XE-4   a declared outage and a first time-zone slip excuse a miss; one emergency excuse in 30 days.
XE-6   a learner's daily cap and future-bookings cap; a partner's daily cap.
XE-7   household links are never matched.
XE-10  webhook signatures; events stored once; presence merged and counted.
XE-11  severity and interim suspension; strike escalation; appeals by someone else.
XE-12  disputes carry the session's evidence; a retaliatory rating can be removed.
XE-16/17  pod rules (lone woman, same-gender, age span, birth date needed).
XE-19  the voice-note queue: claim, review, verdict, auto-accept.
XE-20  three contact attempts in 30 days open one automatic case.
XE-21  recording defaults: on for partners, off for learners.
XE-22/23  Phase 0 learners and guests: Foundation only, lesson-0 cards, behind their flags.
XE-24  pay shares; nothing accrues without paid_partners and a paid role.
"""
import base64
import hashlib
import hmac
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from app.core.ethiopian_clock import both_clocks, ethiopian_clock
from app.services import exchange_case_service as cases
from app.services import exchange_group_service as groups
from app.services import exchange_pay_service as pay
from app.services import exchange_presence_service as presence
from app.services import exchange_recording_service as rec
from app.services import exchange_service as ex
from app.services import exchange_voice_service as voice
from app.services.exchange_service import ExchangeRefused
from tests.fakedb import FakeDB
from tests.test_exchange import L, NOW, P1, P2, SAT, _db, _partner

U = "00000000-0000-4000-8000-0000000000"


@pytest.fixture
def db():
    fake = _db()
    fake.rows["feature_flag"] += [{"key": k, "status": "ready"} for k in (
        "exchange_cases", "exchange_groups", "exchange_voice_review", "exchange_recording", "exchange_guests",
        "exchange_phase0", "paid_partners", "exchange_contact_cases", "exchange_practise_now")]
    for t in ("exchange_sanction", "exchange_appeal", "exchange_dispute", "exchange_household_link", "exchange_outage",
              "exchange_provider_event", "exchange_presence", "exchange_group", "exchange_group_member",
              "exchange_sensitive", "exchange_match_pref", "exchange_recording", "exchange_voice_note",
              "exchange_earning", "exchange_rate", "exchange_language_level", "exchange_host_grant", "exchange_series"):
        fake.rows.setdefault(t, [])
    fake.unique["exchange_provider_event"] = [("provider", "event_id")]
    fake.unique["exchange_appeal"] = [("sanction_id",)]
    fake.unique["exchange_earning"] = [("partner_id", "kind", "ref_id")]
    fake.rows["exchange_voice_prompt"] = [{"id": "vp1", "band": "foundation", "prompt": "Say hello", "active": True}]
    fake.rows["exchange_partner_hour"] = [{"user_id": P1, "day_of_week": 5, "hour": 13},
                                          {"user_id": P2, "day_of_week": 5, "hour": 13}]
    mods = ["exchange_service", "exchange_case_service", "exchange_group_service", "exchange_pay_service",
            "exchange_presence_service", "exchange_recording_service", "exchange_voice_service", "exchange_member_service"]
    patches = [patch(f"app.services.{m}.db", fake) for m in mods]
    patches += [patch("app.core.flags.db", fake), patch("app.services.gender_service.db", fake),
                patch("app.services.exchange_service._notify", new=AsyncMock()),
                patch("app.services.hierarchy_service.admin_can", side_effect=lambda actor, cap: actor.startswith("staff"))]
    for p in patches:
        p.start()
    yield fake
    for p in patches:
        p.stop()


def _booked(db, partner=P1, at=SAT, **kw):
    row = {"id": f"s{len(db.rows['exchange_session']) + 1}", "learner_id": L, "partner_id": partner, "kind": "scheduled",
           "scheduled_at": at.isoformat(), "length_min": 30, "status": "booked", "lesson_number": 1, "connect_failures": 0}
    row.update(kw)
    db.rows["exchange_session"].append(row)
    return row


# XE-1
def test_ethiopian_clock():
    vectors = {(6, 0): (12, 0, "morning"), (7, 30): (1, 30, "morning"), (12, 0): (6, 0, "afternoon"),
               (18, 0): (12, 0, "night"), (19, 0): (1, 0, "night"), (0, 0): (6, 0, "late night"),
               (5, 59): (11, 59, "late night")}
    for (h, m), want in vectors.items():
        at = datetime(2027, 1, 1, h, m, tzinfo=timezone(timedelta(hours=3)))
        assert ethiopian_clock(at) == want, (h, m)
    assert both_clocks(datetime(2027, 1, 1, 16, 0, tzinfo=timezone.utc)) == "19:00 EAT · Ethiopian time 1:00 night (ማታ)"


# XE-3
def test_local_numbers_and_spelled_numbers(db):
    assert ex.mask_contacts("call 0712 345 678")[1]
    assert ex.mask_contacts("mine is +251 911 234 567")[1]
    assert not ex.mask_contacts("lesson 12 at 7")[1]
    db.rows["feature_flag"].append({"key": "exchange_message_flags", "status": "ready"})
    db.rows["conversations"] = [{"id": "c1", "type": "exchange"}]
    db.rows["conversation_participants"] = [{"conversation_id": "c1", "user_id": L}, {"conversation_id": "c1", "user_id": P1}]
    text, info = ex.filter_message("c1", P1, "zero seven one two three four five six")
    assert "spelled_number" in info["flags"] and text.startswith("zero")       # flagged, never masked


# XE-4
def test_outage_and_first_slip_excuse(db):
    next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["country_code"] = "ET"
    db.rows["exchange_outage"].append({"country_code": "ET", "starts_at": (SAT - timedelta(hours=1)).isoformat(),
                                       "ends_at": (SAT + timedelta(hours=1)).isoformat()})
    s = _booked(db)
    ex._settle_miss(s, P1, SAT + timedelta(days=1))
    assert db.rows["exchange_session"][0]["excused_reason"] == "force_majeure"
    assert ex.misses(P1, SAT + timedelta(days=1)) == 0
    db.rows["exchange_outage"] = []
    s2 = _booked(db, partner=P2, at=SAT + timedelta(days=7))
    ex._settle_miss(s2, P2, SAT + timedelta(days=8))
    assert db.rows["exchange_session"][1]["excused_reason"] == "first_timezone"
    s3 = _booked(db, partner=P2, at=SAT + timedelta(days=9))
    ex._settle_miss(s3, P2, SAT + timedelta(days=10))
    assert not db.rows["exchange_session"][2].get("excused_reason")            # only the first is forgiven
    assert ex.excuse(P2, s3["id"], now=SAT + timedelta(days=10))["ok"]
    s4 = _booked(db, partner=P2, at=SAT + timedelta(days=11), status="missed", missed_by=P2)
    with pytest.raises(ExchangeRefused, match="excuse_used"):
        ex.excuse(P2, s4["id"], now=SAT + timedelta(days=12))


# XE-6, XE-7
def test_caps_and_households(db):
    db.rows["config"].append({"key": "exchange.caps", "value": {"partner_per_day": 1, "learner_per_day": 2, "learner_future": 6}})
    _booked(db, partner=P1, at=SAT - timedelta(hours=2))
    assert ex.candidates(L, SAT, 30, NOW) == [P2]                              # P1 already has one that day
    db.rows["config"] = [c for c in db.rows["config"] if c["key"] != "exchange.caps"]
    db.rows["exchange_household_link"].append({"a": L, "b": P2})
    assert P2 not in ex.candidates(L, SAT, 30, NOW)
    db.rows["exchange_household_link"] = []
    _booked(db, partner=P2, at=SAT + timedelta(hours=1))
    with pytest.raises(ExchangeRefused, match="daily_cap"):
        ex.book(L, None, (SAT + timedelta(hours=3)).isoformat(), 30, now=NOW)


# XE-10
def test_webhooks_and_presence(db):
    body = b'{"event":"participant_joined"}'
    with patch("app.services.exchange_presence_service.settings") as st:
        st.LIVEKIT_SECRET, st.LIVEKIT_API_KEY = "sekret", "key"
        tok = jwt.encode({"iss": "key", "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode(),
                          "exp": int(time.time()) + 60}, "sekret", algorithm="HS256")
        assert presence.verify_livekit(body, tok)
        assert not presence.verify_livekit(body + b" ", tok)
        st.daily_webhook_secret = base64.b64encode(b"dk").decode()
        sig = base64.b64encode(hmac.new(b"dk", b"123." + body, hashlib.sha256).digest()).decode()
        assert presence.verify_daily(body, sig, "123") and not presence.verify_daily(body, sig, "124")
    sid = "11111111-2222-4333-8444-555555555555"
    s = _booked(db, id=sid, partner=P1)
    pid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    s["partner_id"] = pid
    db.rows["exchange_provider_event"] = []
    room = {"name": f"ex-{sid.replace('-', '')}"}
    t0 = int(SAT.timestamp())
    assert presence.record("livekit", {"id": "e1", "event": "participant_joined", "createdAt": t0, "room": room,
                                       "participant": {"identity": pid}})
    assert not presence.record("livekit", {"id": "e1", "event": "participant_joined", "createdAt": t0, "room": room,
                                           "participant": {"identity": pid}})     # a repeat is ignored
    assert db.rows["exchange_session"][0]["partner_joined_at"]
    presence.record("livekit", {"id": "e2", "event": "participant_left", "createdAt": t0 + 600, "room": room,
                                "participant": {"identity": pid}})
    presence.record("livekit", {"id": "e3", "event": "participant_joined", "createdAt": t0 + 630, "room": room,
                                "participant": {"identity": pid}})
    presence.record("livekit", {"id": "e4", "event": "participant_left", "createdAt": t0 + 1800, "room": room,
                                "participant": {"identity": pid}})
    assert presence.present_seconds(sid, pid, SAT, SAT + timedelta(minutes=30)) == 1800   # a 30 s gap is merged


# XE-11
def test_cases_sanctions_appeals(db):
    s = _booked(db)
    ex.report(L, s["id"], "sexual_conduct", "said something sexual")
    r = db.rows["concern_report"][0]
    assert r["severity"] == "S3" and r["interim_suspended_at"]
    assert cases.suspended(P1)
    assert P1 not in ex.candidates(L, SAT, 30, NOW)
    for _ in range(2):
        cases.sanction("staff-a", P1, "strike", r["id"], "upheld")
    kinds = [x["kind"] for x in db.rows["exchange_sanction"]]
    assert kinds.count("strike") == 2 and "suspend_7" in kinds                 # 2 active strikes = 7 days off
    strike = next(x for x in db.rows["exchange_sanction"] if x["kind"] == "strike")
    a = cases.appeal(P1, strike["id"], "I didn't")
    with pytest.raises(cases.CaseRefused, match="conflict_of_interest"):
        cases.decide_appeal("staff-a", a["id"], "overturned")
    cases.decide_appeal("staff-b", a["id"], "overturned")
    assert db.rows["exchange_sanction"][[x["id"] for x in db.rows["exchange_sanction"]].index(strike["id"])]["lifted_at"]
    with pytest.raises(cases.CaseRefused, match="not_allowed"):
        cases.cases("ops-person")


# XE-12
def test_disputes(db):
    s = _booked(db, status="missed", missed_by=L)
    d = cases.open_dispute(L, "attendance", s["id"], "I was there")
    assert d["evidence"]["session"]["status"] == "missed"
    with pytest.raises(cases.CaseRefused, match="not_your_session"):
        cases.open_dispute(P2, "attendance", s["id"], "?")
    db.rows["exchange_rating"].append({"session_id": s["id"], "rater_id": P1, "helpfulness": 1})
    r = cases.open_dispute(L, "rating", s["id"], "retaliation")
    cases.decide_dispute("staff-a", r["id"], "remove_rating", "given after a report")
    assert not [x for x in db.rows["exchange_rating"] if x["session_id"] == s["id"]]


# XE-20
@pytest.mark.asyncio
async def test_contact_attempts_open_one_case(db):
    db.rows["conversations"] = [{"id": "c1", "type": "exchange"}]
    db.rows["conversation_participants"] = [{"conversation_id": "c1", "user_id": L}, {"conversation_id": "c1", "user_id": P1}]
    with patch("app.services.concern_service.alert_new", new=AsyncMock()):
        for _ in range(4):
            _, info = ex.filter_message("c1", P1, "0712 345 678")
            assert info["masked"] and "contact_attempt" in info["flags"]
            await ex.record_flags("c1", None, P1, info["flags"])
    auto = [r for r in db.rows["concern_report"] if r.get("automatic")]
    assert len(auto) == 1 and auto[0]["about_user_id"] == P1 and auto[0]["severity"] == "S2"


# XE-16 / XE-17
def test_pod_rules():
    w = {"gender": "woman", "same_pod": None, "age_band": 1, "any_mix": False, "widen": False}
    m = {"gender": "man", "same_pod": None, "age_band": 1, "any_mix": False, "widen": False}
    assert groups.pod_ok({"a": w, "b": m, "c": m}) == "lone_woman"
    assert groups.pod_ok({"a": {**w, "any_mix": True}, "b": m}) is None
    assert groups.pod_ok({"a": w, "b": w, "c": m}) is None
    assert groups.pod_ok({"a": {**m, "same_pod": "requirement"}, "b": w, "c": w}) == "same_gender_only"
    assert groups.pod_ok({"a": {**m, "age_band": 0}, "b": {**m, "age_band": 2}}) == "age_span"
    assert groups.pod_ok({"a": {**m, "age_band": 0, "widen": True}, "b": {**m, "age_band": 2, "widen": True}}) is None
    assert groups.pod_ok({"a": {**m, "age_band": None}, "b": m}) == "needs_birth_date"
    assert groups.age_band(groups.age_on(date(2000, 6, 1), date(2027, 1, 1))) == 1


def test_details_never_return_the_date(db):
    out = groups.set_details(L, "1990-05-04", widen_age_band=True)
    assert out == {"has_birth_date": True, "age_band": "26-40", "widen_age_band": True, "pod_any_mix": False}
    with pytest.raises(groups.GroupRefused, match="under_18"):
        groups.set_details(L, (date.today().replace(year=date.today().year - 16)).isoformat())


# XE-19
def test_voice_queue(db):
    next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["roles"] = ["reviewer"]
    with patch("app.services.exchange_voice_service._signed", return_value="url"):
        n = voice.submit(L, "vp1", f"speech/{L}/a.webm")
        with pytest.raises(voice.VoiceRefused, match="bad_path"):
            voice.submit(L, "vp1", "speech/someone-else/a.webm")
        with pytest.raises(voice.VoiceRefused, match="not_a_reviewer"):
            voice.claim(P2)
        got = voice.claim(P1, now=NOW)
        assert got["id"] == n["id"]
        with pytest.raises(voice.VoiceRefused, match="incomplete"):
            voice.review(P1, n["id"], f"speech/{P1}/r.webm", "Selam", [], "Tena yistilign")
        voice.review(P1, n["id"], f"speech/{P1}/r.webm", "Selam new", ["the ending"], "Tena yistilign", now=NOW)
        out = voice.hourly(NOW + timedelta(hours=73))
    assert out["accepted"] == 1 and db.rows["exchange_voice_note"][0]["status"] == "accepted"


# XE-21
def test_recording_defaults(db):
    assert rec.prefs(P1)["record_own_side"] is True                            # partners: on by default
    assert rec.prefs(L)["record_own_side"] is False                            # learners: off (D-1)
    out = rec.set_prefs(L, True, None, None)
    assert out["record_own_side"] and db.rows["consent_record"][-1]["purpose"] == "exchange_recording"


# XE-22 / XE-23
def test_phase0_and_guests(db):
    db.rows["enrolment_state"][0]["state"] = "phase0"
    assert ex.eligible(L) and ex.foundation_only(L) and ex._lesson(L) == 0
    db.rows["feature_flag"] = [f for f in db.rows["feature_flag"] if f["key"] != "exchange_phase0"]
    assert ex.eligible(L)                       # with guests on, Phase 0 learners are in too
    guests = [f for f in db.rows["feature_flag"] if f["key"] == "exchange_guests"]
    db.rows["feature_flag"] = [f for f in db.rows["feature_flag"] if f["key"] != "exchange_guests"]
    assert not ex.eligible(L)
    db.rows["feature_flag"] += guests
    db.rows["student"][0]["tier"] = "free_tier"
    db.rows["enrolment_state"][0].update({"id": "e1", "state": "exchange_guest"})
    assert ex.eligible(L) and ex._is_guest(L)
    db.rows["peer_task"].append({"id": "f0", "side": "learner", "lesson_number": 0, "kind": "role_play", "title": "Hello",
                                 "prompt_a": "Greet", "prompt_b": "Answer", "active": True, "created_at": "2027-01-01"})
    lcard, _ = ex._cards({"lesson_number": 0, "partner_id": P1})
    assert lcard["id"] == "f0"
    with pytest.raises(ExchangeRefused, match="bad_length"):
        db.rows["enrolment_state"][0]["state"] = "in_pod"
        ex.book(L, None, SAT.isoformat(), 15, now=NOW)                         # tasters are Foundation only



def test_a_paid_guest_is_a_guest_no_longer(db):
    # 2026-11-07: however the plan was bought, the next read moves a paying guest to Phase 0.
    from app.services import exchange_member_service as members
    db.rows["enrolment_state"][0].update({"id": "e1", "state": "exchange_guest"})
    db.rows["student"][0]["tier"] = "free_tier"
    assert members.guest_state(L)["guest"] is True
    assert members.graduate_paid_guests() == 0
    db.rows["student"][0]["tier"] = "learner_tier"
    assert members.graduate_paid_guests() == 1
    row = db.rows["enrolment_state"][0]
    assert row["state"] == "phase0" and row["phase0_started_at"]
    assert members.guest_state(L)["guest"] is False and ex.eligible(L)


# XE-24
def test_pay(db):
    assert pay.pay_share(1800, 1800, 0) == 1.0
    assert pay.pay_share(1800, 1800, 400) == 0.75
    assert pay.pay_share(1200, 1800, 0) == 0.67
    assert pay.pay_share(600, 1800, 0) == 0.0
    s = _booked(db, status="completed", partner_joined_at=SAT.isoformat())
    assert pay.accrue_pair(s, NOW) is None                                     # P1 has no paid role
    next(p for p in db.rows["exchange_partner"] if p["user_id"] == P1)["roles"] = ["pap"]
    e = pay.accrue_pair(s, NOW)
    assert e["amount_minor"] == 20000 and e["kind"] == "live"                  # Foundation (lesson 1), 30 min
    assert pay.accrue_pair(s, NOW) is None                                     # once per session
    assert pay.release(NOW + timedelta(days=8)) == 1
