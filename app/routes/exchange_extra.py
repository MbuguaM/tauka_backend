"""Exchange extension routes (§159; exchange-extension-plan.md). Mounted at /exchange beside
routes/exchange.py, plus the provider webhooks at /webhooks/*.

Every route checks its own flag in the service; with the flags 'coming' they answer
`not_enabled` (or an empty state) and change nothing.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import exchange_case_service as cases
from app.services import exchange_group_service as groups
from app.services import exchange_member_service as members
from app.services import exchange_pay_service as pay
from app.services import exchange_presence_service as presence
from app.services import exchange_recording_service as rec
from app.services import exchange_service as svc
from app.services import exchange_voice_service as voice

logger = logging.getLogger(__name__)
router = APIRouter()
webhooks = APIRouter()

UUID = r"^[0-9a-fA-F-]{36}$"
REFUSALS = (svc.ExchangeRefused, cases.CaseRefused, groups.GroupRefused, members.MemberRefused, pay.PayRefused,
            rec.RecordingRefused, voice.VoiceRefused)
_STATUS = {"not_enabled": 403, "not_allowed": 403, "not_your_session": 403, "not_a_host": 403, "not_a_reviewer": 403,
           "not_a_paid_partner": 403, "suspended": 409, "not_eligible": 409, "allowance_used": 409, "full": 409,
           "overlap": 409, "unavailable": 409, "needs_birth_date": 409, "under_18": 409, "excuse_used": 409,
           "nothing_to_excuse": 409, "too_late": 409, "already_appealed": 409, "not_open": 409, "not_now": 409,
           "daily_cap": 409, "not_configured": 503, "not_in_market": 409, "no_room": 503}


def _run(fn, *a, **k):
    try:
        return fn(*a, **k)
    except REFUSALS as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


async def _arun(fn, *a, **k):
    try:
        return await fn(*a, **k)
    except REFUSALS as exc:
        raise HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


# ── XE-4 leniency, XE-12 disputes, XE-11 sanctions and appeals ─────────────

@router.post("/sessions/{session_id}/excuse")
async def excuse(session_id: str, user_id: str = Depends(get_current_user)):
    """One self-declared emergency (power cut, illness) per 30 days removes a miss."""
    return _run(svc.excuse, user_id, session_id)


class Dispute(BaseModel):
    kind: Literal["attendance", "credits", "rating", "quality", "placement", "pay"]
    session_id: Optional[str] = Field(None, pattern=UUID)
    statement: str = Field(..., min_length=1, max_length=2000)


@router.post("/disputes")
async def open_dispute(req: Dispute, user_id: str = Depends(get_current_user)):
    return _run(cases.open_dispute, user_id, req.kind, req.session_id, req.statement)


@router.get("/disputes")
async def my_disputes(user_id: str = Depends(get_current_user)):
    return {"disputes": cases.my_disputes(user_id)}


@router.get("/sanctions")
async def my_sanctions(user_id: str = Depends(get_current_user)):
    return {"sanctions": cases.my_sanctions(user_id)}


class Appeal(BaseModel):
    statement: str = Field(..., min_length=1, max_length=2000)


@router.post("/sanctions/{sanction_id}/appeal")
async def appeal(sanction_id: str, req: Appeal, user_id: str = Depends(get_current_user)):
    return _run(cases.appeal, user_id, sanction_id, req.statement)


# ── XE-17 details, XE-21 recording preferences ────────────────────────────

class Details(BaseModel):
    date_of_birth: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    widen_age_band: Optional[bool] = None
    pod_any_mix: Optional[bool] = None


@router.get("/me/details")
async def details(user_id: str = Depends(get_current_user)):
    return groups.details(user_id)


@router.post("/me/details")
async def set_details(req: Details, user_id: str = Depends(get_current_user)):
    return _run(groups.set_details, user_id, req.date_of_birth, req.widen_age_band, req.pod_any_mix)


class RecordingPrefs(BaseModel):
    record_own_side: Optional[bool] = None
    recap_opt_in: Optional[bool] = None
    wifi_only_upload: Optional[bool] = None


@router.get("/me/recording")
async def recording_prefs(user_id: str = Depends(get_current_user)):
    return rec.prefs(user_id)


@router.post("/me/recording")
async def set_recording_prefs(req: RecordingPrefs, user_id: str = Depends(get_current_user)):
    return rec.set_prefs(user_id, req.record_own_side, req.recap_opt_in, req.wifi_only_upload)


class RecordingCall(BaseModel):
    kind: Literal["pair", "group"] = "pair"
    duration_s: Optional[int] = Field(None, ge=0, le=14400)


@router.post("/recordings/{session_id}/start")
async def recording_start(session_id: str, req: RecordingCall, user_id: str = Depends(get_current_user)):
    return _run(rec.start, user_id, req.kind, session_id)


@router.post("/recordings/{session_id}/complete")
async def recording_complete(session_id: str, req: RecordingCall, user_id: str = Depends(get_current_user)):
    return _run(rec.complete, user_id, req.kind, session_id, req.duration_s)


@router.post("/recordings/{session_id}/discard")
async def recording_discard(session_id: str, req: RecordingCall, user_id: str = Depends(get_current_user)):
    return _run(rec.discard, user_id, req.kind, session_id)


# ── XE-18 provider fault ──────────────────────────────────────────────────

@router.post("/sessions/{session_id}/provider-fault")
async def provider_fault(session_id: str, user_id: str = Depends(get_current_user)):
    return await presence.provider_fault(user_id, session_id)


# ── XE-16 groups ──────────────────────────────────────────────────────────

class NewGroup(BaseModel):
    kind: Literal["foundation_support", "peer_guide", "fluent_english"]
    starts_at: str = Field(..., max_length=40)
    length_min: Literal[30, 45] = 30
    band: Literal["foundation", "conversation", "advanced"]
    capacity: int = Field(3, ge=2, le=3)
    weeks: Optional[int] = Field(None, ge=4, le=8)


@router.get("/groups")
async def list_groups(user_id: str = Depends(get_current_user)):
    return {"groups": _run(groups.open_groups, user_id), "mine": groups.mine(user_id)}


@router.post("/groups")
async def create_group(req: NewGroup, user_id: str = Depends(get_current_user)):
    return {"groups": _run(groups.create, user_id, req.kind, req.starts_at, req.length_min, req.band, req.capacity, req.weeks)}


@router.post("/groups/{group_id}/book")
async def book_group(group_id: str, user_id: str = Depends(get_current_user)):
    return _run(groups.book, user_id, group_id)


@router.post("/groups/{group_id}/leave")
async def leave_group(group_id: str, user_id: str = Depends(get_current_user)):
    return _run(groups.leave, user_id, group_id)


@router.post("/groups/{group_id}/join")
async def join_group(group_id: str, user_id: str = Depends(get_current_user)):
    return await _arun(groups.join, user_id, group_id)


# ── XE-19 voice notes ─────────────────────────────────────────────────────

@router.get("/voice")
async def voice_state(user_id: str = Depends(get_current_user)):
    try:
        mine = voice.mine(user_id)
        p = voice.prompts(user_id)
    except voice.VoiceRefused as exc:
        return {"enabled": False, "reason": exc.reason}
    return {"enabled": True, **p, "notes": mine, "reviewer": voice.reviewer_state(user_id)}


class VoiceSubmit(BaseModel):
    prompt_id: str = Field(..., pattern=UUID)
    audio_path: str = Field(..., max_length=200)


@router.post("/voice")
async def voice_submit(req: VoiceSubmit, user_id: str = Depends(get_current_user)):
    return _run(voice.submit, user_id, req.prompt_id, req.audio_path)


class Verdict(BaseModel):
    accept: bool
    reason: Optional[str] = Field(None, max_length=300)


@router.post("/voice/{note_id}/verdict")
async def voice_verdict(note_id: str, req: Verdict, user_id: str = Depends(get_current_user)):
    return _run(voice.verdict, user_id, note_id, req.accept, req.reason)


@router.post("/voice/claim")
async def voice_claim(user_id: str = Depends(get_current_user)):
    return {"note": _run(voice.claim, user_id)}


class Review(BaseModel):
    audio_path: str = Field(..., max_length=200)
    corrected: str = Field(..., min_length=1, max_length=1000)
    fixes: list[str] = Field(..., min_length=1, max_length=2)
    alternative: str = Field(..., min_length=1, max_length=500)


@router.post("/voice/{note_id}/review")
async def voice_review(note_id: str, req: Review, user_id: str = Depends(get_current_user)):
    return _run(voice.review, user_id, note_id, req.audio_path, req.corrected, req.fixes, req.alternative)


# ── XE-24 pay, XE-25 Exchange+, certificate, XE-23 guests ────────────────

@router.get("/partner/earnings")
async def earnings(user_id: str = Depends(get_current_user)):
    return pay.mine(user_id)


class Destination(BaseModel):
    msisdn: str = Field(..., min_length=9, max_length=16)
    registered_name: str = Field(..., min_length=2, max_length=120)


@router.post("/partner/payout-destination")
async def payout_destination(req: Destination, user_id: str = Depends(get_current_user)):
    return _run(pay.set_destination, user_id, req.msisdn, req.registered_name)


@router.get("/plus")
async def plus(user_id: str = Depends(get_current_user)):
    return members.plus_state(user_id)


@router.post("/plus/checkout")
async def plus_checkout(user_id: str = Depends(get_current_user)):
    return await _arun(members.plus_checkout, user_id)


class Confirm(BaseModel):
    reference: str = Field(..., max_length=80)


@router.post("/plus/confirm")
async def plus_confirm(req: Confirm, user_id: str = Depends(get_current_user)):
    return await _arun(members.plus_confirm, user_id, req.reference)


@router.get("/certificate")
async def certificate(user_id: str = Depends(get_current_user)):
    return members.certificate(user_id)


class GuestJoin(BaseModel):
    first_name: Optional[str] = Field(None, max_length=120)
    age_confirmed: bool


@router.post("/guest/join")
async def guest_join(req: GuestJoin, user_id: str = Depends(get_current_user)):
    return await _arun(members.join_guest, user_id, req.first_name, req.age_confirmed)


@router.get("/guest")
async def guest(user_id: str = Depends(get_current_user)):
    return members.guest_state(user_id)


# ── ops and safeguarding ──────────────────────────────────────────────────

@router.get("/admin/cases")
async def admin_cases(user_id: str = Depends(get_current_user)):
    return {"cases": _run(cases.cases, user_id), "appeals": _run(cases.appeals, user_id)}


class Severity(BaseModel):
    severity: Literal["S1", "S2", "S3", "S4"]


@router.post("/admin/cases/{concern_id}/severity")
async def admin_severity(concern_id: str, req: Severity, user_id: str = Depends(get_current_user)):
    return _run(cases.set_severity, user_id, concern_id, req.severity)


class Sanction(BaseModel):
    user_id: str = Field(..., pattern=UUID)
    kind: Literal["nudge", "warning", "strike", "suspend_7", "suspend_30", "suspend_90", "ban", "role_removal"]
    concern_id: Optional[str] = Field(None, pattern=UUID)
    reason: Optional[str] = Field(None, max_length=1000)


@router.post("/admin/sanctions")
async def admin_sanction(req: Sanction, user_id: str = Depends(get_current_user)):
    return {"made": _run(cases.sanction, user_id, req.user_id, req.kind, req.concern_id, req.reason)}


@router.post("/admin/sanctions/{sanction_id}/lift")
async def admin_lift(sanction_id: str, user_id: str = Depends(get_current_user)):
    return _run(cases.lift, user_id, sanction_id)


class AppealDecision(BaseModel):
    outcome: Literal["upheld", "reduced", "overturned"]


@router.post("/admin/appeals/{appeal_id}")
async def admin_appeal(appeal_id: str, req: AppealDecision, user_id: str = Depends(get_current_user)):
    return _run(cases.decide_appeal, user_id, appeal_id, req.outcome)


@router.get("/admin/cases/{concern_id}/recordings")
async def admin_case_recordings(concern_id: str, user_id: str = Depends(get_current_user)):
    return {"recordings": _run(rec.for_case, user_id, concern_id)}


@router.post("/admin/cases/{concern_id}/recordings/{recording_id}/open")
async def admin_open_recording(concern_id: str, recording_id: str, user_id: str = Depends(get_current_user)):
    return {"url": _run(rec.open_for_case, user_id, recording_id, concern_id)}


@router.get("/admin/disputes")
async def admin_disputes(user_id: str = Depends(get_current_user)):
    return {"disputes": _run(cases.disputes, user_id)}


class DisputeDecision(BaseModel):
    action: Literal["resolve", "escalate", "remove_rating"]
    decision: str = Field(..., min_length=1, max_length=2000)


@router.post("/admin/disputes/{dispute_id}")
async def admin_dispute(dispute_id: str, req: DisputeDecision, user_id: str = Depends(get_current_user)):
    return _run(cases.decide_dispute, user_id, dispute_id, req.action, req.decision)


def _ops(user_id: str) -> None:
    from app.services import hierarchy_service as hs
    if not hs.admin_can(user_id, "ops"):
        raise HTTPException(status_code=403, detail={"reason": "not_allowed"})


class Outage(BaseModel):
    country_code: str = Field(..., pattern=r"^[A-Z]{2}$")
    starts_at: str = Field(..., max_length=40)
    ends_at: str = Field(..., max_length=40)
    note: Optional[str] = Field(None, max_length=300)


@router.post("/admin/outages")
async def admin_outage(req: Outage, user_id: str = Depends(get_current_user)):
    """XE-4: a declared outage (an internet shutdown) excuses misses in that country and window."""
    _ops(user_id)
    row = svc._t("exchange_outage").insert({"country_code": req.country_code, "starts_at": req.starts_at,
                                            "ends_at": req.ends_at, "note": req.note, "declared_by": user_id}).execute()
    return row.data[0]


class Household(BaseModel):
    a: str = Field(..., pattern=UUID)
    b: str = Field(..., pattern=UUID)


@router.post("/admin/households")
async def admin_household(req: Household, user_id: str = Depends(get_current_user)):
    """XE-7: two accounts of one household are never matched."""
    _ops(user_id)
    a, b = sorted([req.a, req.b])
    if a == b:
        raise HTTPException(status_code=400, detail={"reason": "bad_value"})
    svc._t("exchange_household_link").upsert({"a": a, "b": b, "created_by": user_id}).execute()
    return {"ok": True}


class Grant(BaseModel):
    user_id: str = Field(..., pattern=UUID)
    role: Literal["peer_guide", "alumni_host"]
    on: bool = True


@router.post("/admin/host-grants")
async def admin_grant(req: Grant, user_id: str = Depends(get_current_user)):
    _ops(user_id)
    if req.on:
        svc._t("exchange_host_grant").upsert({"user_id": req.user_id, "role": req.role, "granted_by": user_id}).execute()
    else:
        svc._t("exchange_host_grant").delete().eq("user_id", req.user_id).eq("role", req.role).execute()
    return {"ok": True}


class Roles(BaseModel):
    roles: list[Literal["barter", "pap", "fep", "reviewer"]] = Field(..., min_length=1, max_length=4)


@router.post("/admin/partners/{partner_id}/roles")
async def admin_roles(partner_id: str, req: Roles, user_id: str = Depends(get_current_user)):
    return _run(pay.set_roles, user_id, partner_id, req.roles)


class Rate(BaseModel):
    work_type: Literal["live_30", "live_45", "voice_review", "recording_min"]
    band: Literal["foundation", "conversation", "advanced"]
    amount_minor: int = Field(..., ge=0)
    effective_from: str = Field(..., max_length=40)


@router.post("/admin/rates")
async def admin_rate(req: Rate, user_id: str = Depends(get_current_user)):
    return _run(pay.set_rate, user_id, req.work_type, req.band, req.amount_minor, req.effective_from)


@router.post("/admin/partners/{partner_id}/payout-destination/verify")
async def admin_verify_destination(partner_id: str, user_id: str = Depends(get_current_user)):
    return _run(pay.verify_destination, user_id, partner_id)


@router.get("/admin/payout-batches")
async def admin_batches(user_id: str = Depends(get_current_user)):
    return {"batches": _run(pay.batches, user_id)}


@router.post("/admin/payout-batches/{batch_id}/approve")
async def admin_approve(batch_id: str, user_id: str = Depends(get_current_user)):
    return _run(pay.approve, user_id, batch_id)


@router.post("/admin/payout-batches/{batch_id}/send")
async def admin_send(batch_id: str, user_id: str = Depends(get_current_user)):
    return await _arun(pay.send, user_id, batch_id)


@router.get("/admin/voice-sample")
async def admin_voice_sample(user_id: str = Depends(get_current_user)):
    return {"notes": _run(voice.sample, user_id)}


@router.get("/admin/minute-budget")
async def admin_budget(user_id: str = Depends(get_current_user)):
    _ops(user_id)
    return {"pools": (svc._t("exchange_minute_pool").select("*").execute()).data or []}


# ── XE-10 provider webhooks ───────────────────────────────────────────────

@webhooks.post("/livekit")
async def livekit_webhook(request: Request, authorization: str | None = Header(default=None)):
    body = await request.body()
    if not presence.verify_livekit(body, authorization):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        presence.record("livekit", await request.json())
    except Exception as exc:  # noqa: BLE001
        logger.error("livekit webhook failed: %s", exc)
        raise HTTPException(status_code=500, detail="not recorded")   # the provider retries
    return {"ok": True}


@webhooks.post("/daily")
async def daily_webhook(request: Request, x_webhook_signature: str | None = Header(default=None),
                        x_webhook_timestamp: str | None = Header(default=None)):
    body = await request.body()
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if payload.get("test") == "test":               # Daily's endpoint check when the webhook is created
        return {"ok": True}
    if not presence.verify_daily(body, x_webhook_signature, x_webhook_timestamp):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        presence.record("daily", payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("daily webhook failed: %s", exc)
        raise HTTPException(status_code=500, detail="not recorded")
    return {"ok": True}
