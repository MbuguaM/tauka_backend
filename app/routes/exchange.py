"""Exchange partners (features addendum A, §142), behind the exchange_partners flag.

Learner: `GET /exchange`, `GET /exchange/slots`, `POST /exchange/book`,
`POST /exchange/favourites`.
Partner: `GET /exchange/partner`, `POST /exchange/partner/join`, `…/profile`,
`…/english-sample`, `…/availability`, `…/conduct`, `GET …/training`,
`POST …/training`, `POST …/activate`, `POST /exchange/sessions/{id}/respond`,
`POST …/english-practice`, `GET …/phrases`, `GET …/export`.
Either side: `POST /exchange/sessions/{id}/join|connect-failed|ended|rate|report|block|cancel`,
`POST /exchange/now {side}`, `GET /exchange/now`, `POST /exchange/now/leave`.
Ops / safeguarding / finance: `/exchange/admin/*`.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services import exchange_service as svc
from app.services.exchange_service import ExchangeRefused

router = APIRouter()
_STATUS = {"not_enabled": 403, "not_allowed": 403, "not_your_session": 403, "not_your_request": 403,
           "not_a_partner": 403, "removed": 403, "not_eligible": 409, "paused": 409, "allowance_used": 409,
           "no_partner": 409, "not_now": 409, "not_open": 409, "too_late": 409, "not_started": 409,
           "already_rated": 409, "not_due": 409, "learner_cannot_partner": 409, "not_in_pilot": 409,
           "not_active": 409, "too_soon": 409, "not_your_partner": 409, "not_a_favourite": 409,
           "no_room": 503, "rate_limited": 429, "not_found": 404}


def _refused(exc: ExchangeRefused):
    code = 409 if exc.reason.startswith("incomplete:") else _STATUS.get(exc.reason, 400)
    raise HTTPException(status_code=code, detail={"reason": exc.reason})


def _run(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ExchangeRefused as exc:
        _refused(exc)


async def _arun(fn, *a, **k):
    try:
        return await fn(*a, **k)
    except ExchangeRefused as exc:
        _refused(exc)


Category = Literal["harassment", "contact_details", "inappropriate_content", "wellbeing", "safety", "other"]
Level = Literal["A2", "B1", "B2", "C1"]


class Join(BaseModel):
    first_name: Optional[str] = Field(None, max_length=120)
    age_confirmed: bool
    region: Optional[str] = Field(None, max_length=80)


class Profile(BaseModel):
    amharic_variety: Literal["standard", "regional"] = "standard"
    variety_note: Optional[str] = Field(None, max_length=120)
    english_self: Optional[Level] = None
    interests: list[str] = Field(default_factory=list, max_length=10)
    low_data: Optional[bool] = None


class Sample(BaseModel):
    path: str = Field(..., max_length=200)


class Availability(BaseModel):
    slot_ids: list[str] = Field(default_factory=list, max_length=21)


class Training(BaseModel):
    answers: dict[str, int]


class Book(BaseModel):
    slot_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    at: str = Field(..., max_length=40)
    length_min: Literal[20, 30] = 30
    partner_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class Favourite(BaseModel):
    partner_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    on: bool


class Respond(BaseModel):
    accept: bool


class Ended(BaseModel):
    duration_s: int = Field(..., ge=0, le=4 * 3600)
    video_used: bool = False
    relay_used: Optional[bool] = None


class Rate(BaseModel):
    helpfulness: int = Field(..., ge=1, le=5)
    again: bool
    other_came: bool = True
    flag_moment_s: Optional[int] = Field(None, ge=0, le=3600)
    note: Optional[str] = Field(None, max_length=500)
    phrase: Optional[str] = Field(None, max_length=200)
    meaning: Optional[str] = Field(None, max_length=200)


class Report(BaseModel):
    category: Category
    detail: Optional[str] = Field(None, max_length=1000)


class Cancel(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


class Now(BaseModel):
    side: Literal["learner", "partner"]


class Practice(BaseModel):
    messages: list[dict] = Field(..., max_length=30)


class EnglishLevel(BaseModel):
    level: Level


class Verify(BaseModel):
    passed: bool


class Status(BaseModel):
    status: Literal["active", "waitlisted", "paused", "removed"]
    reason: Optional[str] = Field(None, max_length=300)


class Paid(BaseModel):
    channel: Literal["telebirr", "mpesa_et"]
    amount_minor: int = Field(..., ge=0)
    reference: Optional[str] = Field(None, max_length=120)


class FlagReview(BaseModel):
    outcome: Literal["no_action", "warning_issued", "paused", "removed"]


# ── learner ─────────────────────────────────────────────────────────────────

@router.get("")
async def learner(user_id: str = Depends(get_current_user)):
    return svc.learner_state(user_id)


@router.get("/slots")
async def slots(user_id: str = Depends(get_current_user)):
    return _run(svc.bookable, user_id)


@router.post("/book")
async def book(req: Book, user_id: str = Depends(get_current_user)):
    out = _run(svc.book, user_id, req.slot_id, req.at, req.length_min, req.partner_id)
    await svc.notify_request(out["id"])
    return out


@router.post("/favourites")
async def favourite(req: Favourite, user_id: str = Depends(get_current_user)):
    return _run(svc.favourite, user_id, req.partner_id, req.on)


# ── partner ─────────────────────────────────────────────────────────────────

@router.get("/partner")
async def partner(user_id: str = Depends(get_current_user)):
    return svc.partner_state(user_id)


@router.post("/partner/join")
async def join(req: Join, user_id: str = Depends(get_current_user)):
    return _run(svc.join_partner, user_id, req.first_name, req.age_confirmed, req.region)


@router.post("/partner/profile")
async def profile(req: Profile, user_id: str = Depends(get_current_user)):
    return _run(svc.set_profile, user_id, req.amharic_variety, req.variety_note, req.english_self, req.interests,
                req.low_data)


@router.post("/partner/english-sample")
async def english_sample(req: Sample, user_id: str = Depends(get_current_user)):
    return _run(svc.set_english_sample, user_id, req.path)


@router.get("/partner/slots")
async def partner_slots(user_id: str = Depends(get_current_user)):
    return {"slots": svc.catalogue()}


@router.post("/partner/availability")
async def availability(req: Availability, user_id: str = Depends(get_current_user)):
    return _run(svc.set_availability, user_id, req.slot_ids)


@router.post("/partner/conduct")
async def conduct(user_id: str = Depends(get_current_user)):
    return _run(svc.agree_conduct, user_id)


@router.get("/partner/training")
async def training(user_id: str = Depends(get_current_user)):
    return {"questions": svc.training()}


@router.post("/partner/training")
async def finish_training(req: Training, user_id: str = Depends(get_current_user)):
    return _run(svc.finish_training, user_id, req.answers)


@router.post("/partner/activate")
async def activate(user_id: str = Depends(get_current_user)):
    return _run(svc.activate, user_id)


@router.post("/partner/english-practice")
async def english_practice(req: Practice, user_id: str = Depends(get_current_user)):
    return {"reply": await _arun(svc.english_practice, user_id, req.messages)}


@router.get("/partner/phrases")
async def partner_phrases(user_id: str = Depends(get_current_user)):
    return {"phrases": svc.phrases(user_id)}


@router.get("/partner/export")
async def partner_export(user_id: str = Depends(get_current_user)):
    return svc.export(user_id)


# ── either side ─────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/respond")
async def respond(session_id: str, req: Respond, user_id: str = Depends(get_current_user)):
    return await _arun(svc.respond, user_id, session_id, req.accept)


@router.post("/sessions/{session_id}/join")
async def join_call(session_id: str, user_id: str = Depends(get_current_user)):
    return await _arun(svc.join_call, user_id, session_id)


@router.post("/sessions/{session_id}/connect-failed")
async def connect_failed(session_id: str, user_id: str = Depends(get_current_user)):
    return await _arun(svc.connect_failed, user_id, session_id)


@router.post("/sessions/{session_id}/ended")
async def ended(session_id: str, req: Ended, user_id: str = Depends(get_current_user)):
    return _run(svc.ended, user_id, session_id, req.duration_s, req.video_used, req.relay_used)


@router.post("/sessions/{session_id}/rate")
async def rate(session_id: str, req: Rate, user_id: str = Depends(get_current_user)):
    return await _arun(svc.rate, user_id, session_id, req.helpfulness, req.again, req.other_came, req.flag_moment_s,
                       req.note, req.phrase, req.meaning)


@router.post("/sessions/{session_id}/report")
async def report(session_id: str, req: Report, user_id: str = Depends(get_current_user)):
    out = _run(svc.report, user_id, session_id, req.category, req.detail)
    try:
        from app.services.concern_service import alert_new
        await alert_new(out["id"])
    except Exception:  # noqa: BLE001
        pass
    return out


@router.post("/sessions/{session_id}/block")
async def block(session_id: str, user_id: str = Depends(get_current_user)):
    return _run(svc.block, user_id, session_id)


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str, req: Cancel, user_id: str = Depends(get_current_user)):
    return _run(svc.cancel, user_id, session_id, req.reason)


@router.post("/now")
async def practise_now(req: Now, user_id: str = Depends(get_current_user)):
    return _run(svc.practise_now, user_id, req.side)


@router.get("/now")
async def now_status(user_id: str = Depends(get_current_user)):
    return svc.now_status(user_id)


@router.post("/now/leave")
async def leave_now(user_id: str = Depends(get_current_user)):
    svc.leave_queue(user_id)
    return {"ok": True}


# ── ops, safeguarding, finance ──────────────────────────────────────────────

@router.get("/admin/partners")
async def admin_partners(status: Optional[str] = None, user_id: str = Depends(get_current_user)):
    return {"partners": _run(svc.admin_partners, user_id, status)}


@router.get("/admin/partners/{partner_id}/english-sample")
async def admin_sample(partner_id: str, user_id: str = Depends(get_current_user)):
    return {"url": _run(svc.english_sample_url, user_id, partner_id)}


@router.post("/admin/partners/{partner_id}/english")
async def admin_english(partner_id: str, req: EnglishLevel, user_id: str = Depends(get_current_user)):
    return _run(svc.set_english_level, user_id, partner_id, req.level)


@router.post("/admin/partners/{partner_id}/verify")
async def admin_verify(partner_id: str, req: Verify, user_id: str = Depends(get_current_user)):
    return _run(svc.verify, user_id, partner_id, req.passed)


@router.post("/admin/partners/{partner_id}/status")
async def admin_status(partner_id: str, req: Status, user_id: str = Depends(get_current_user)):
    return _run(svc.set_status, user_id, partner_id, req.status, req.reason)


@router.get("/admin/rewards")
async def admin_rewards(status: Optional[str] = None, user_id: str = Depends(get_current_user)):
    return {"rewards": _run(svc.admin_rewards, user_id, status)}


@router.post("/admin/rewards/{reward_id}/paid")
async def admin_paid(reward_id: str, req: Paid, user_id: str = Depends(get_current_user)):
    return _run(svc.mark_paid, user_id, reward_id, req.channel, req.amount_minor, req.reference)


@router.get("/admin/flags")
async def admin_flags(user_id: str = Depends(get_current_user)):
    return {"flags": _run(svc.admin_flags, user_id)}


@router.post("/admin/flags/{flag_id}")
async def admin_review_flag(flag_id: str, req: FlagReview, user_id: str = Depends(get_current_user)):
    return _run(svc.review_flag, user_id, flag_id, req.outcome)


@router.get("/admin/metrics")
async def admin_metrics(user_id: str = Depends(get_current_user)):
    return _run(svc.metrics, user_id)
