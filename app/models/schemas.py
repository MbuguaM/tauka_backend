from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional, Any


# ── Existing models ────────────────────────────────────────────────────────────

class AIRequest(BaseModel):
    prompt: str
    provider: Literal["openai", "deepseek", "gemini"] = "deepseek"
    mode: Literal["chat", "translation", "image_translation"] = "chat"
    target_language: Optional[str] = None
    image_url: Optional[str] = None


class MessageRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1, max_length=4000)


class CallRequest(BaseModel):
    room: str
    vendor: Optional[Literal["daily", "livekit"]] = None


# ── Segment A — Subscriptions ──────────────────────────────────────────────────

class SubscriptionCheckoutRequest(BaseModel):
    tier: Literal["learner", "tutor", "intensive"]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class SubscriptionStatusResponse(BaseModel):
    tier: str
    source: str
    status: str
    current_period_end: Optional[str] = None
    active_gift: Optional[dict] = None


# ── Segment C — Test System ────────────────────────────────────────────────────

# Submitted answers are keyed by question id and carry the structured envelope
# {"mode": <interaction mode>, "value": <mode-specific>} — see QUESTION_SCHEMA.md
# section 5. `value` is a string for choice/select_token, a list for
# multi_choice/order/rank/build, and an object for match/bucket, so this cannot
# be narrowed to dict[str, str]. Shape is enforced by is_answer_correct(), which
# scores an answer of the wrong shape as incorrect rather than raising.
AnswerMap = dict[str, Any]


class Phase1SubmitRequest(BaseModel):
    session_id: str
    answers: AnswerMap = Field(..., min_length=1)


class EmailCaptureRequest(BaseModel):
    session_id: str
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr


class FinalSubmitRequest(BaseModel):
    session_id: str
    answers: AnswerMap = Field(..., min_length=1)


class ShareEventRequest(BaseModel):
    session_id: str
    # ARC-002: the `email` share channel was removed — only whatsapp/copy_link.
    channel: Literal["whatsapp", "copy_link"]
    recipient_count: int = Field(default=1, ge=1, le=100)


class SeedQuestionsRequest(BaseModel):
    language: str = Field(..., min_length=1, max_length=50)
    lesson_range: Optional[tuple[int, int]] = None


class LoadQuestionBankRequest(BaseModel):
    """Load a reviewed, authored bank file into app.test_questions."""
    language: str = Field(..., min_length=1, max_length=50)
    path: str = Field(..., min_length=1, description="Server-side path to the bank JSON")


class TestResultResponse(BaseModel):
    """Mirrors what submit_final returns. breakdown is
    {skill: {correct, total, pct}} — an object per skill, not a number."""
    session_id: str
    cefr_result: str
    total_correct: int
    total_questions: int
    breakdown: dict
    description: str
    name: Optional[str] = None
    completed_at: Optional[str] = None
    language: Optional[str] = None
    share_url: str
    referrer_student_id: Optional[str] = None


# ── Segment D — Supporters ─────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    session_id: str
    note: Optional[str] = Field(None, max_length=2000)


class OptInRequest(BaseModel):
    supporter_id: str


class VisibilityToggleRequest(BaseModel):
    supporter_id: str
    visible: bool


# ── Segment E — Gifts ──────────────────────────────────────────────────────────

class GiftCheckoutRequest(BaseModel):
    supporter_id: str
    duration_months: Literal[1, 3]
    anonymous: bool = False


class GiftRefundRequest(BaseModel):
    gift_id: str
    supporter_email: str


# ── Segment F — Testimonials ───────────────────────────────────────────────────

class TestimonialSubmitRequest(BaseModel):
    request_id: str
    quote_text: str = Field(min_length=20, max_length=500)
    display_name: str = Field(min_length=1, max_length=100)
    display_preference: Literal["full_name", "first_name_initial", "anonymous"]


# ── Segment C.5 — Referrals ────────────────────────────────────────────────────

class CreateReferralRequest(BaseModel):
    language: str = Field(..., min_length=1, max_length=50)
    intent: Optional[Literal["validate", "peer"]] = None
    # ARC-002: the `email` share channel was removed — only whatsapp/copy_link.
    channel: Literal["whatsapp", "copy_link"]
    sender_name: Optional[str] = Field(None, min_length=1, max_length=100)
    sender_email: Optional[EmailStr] = None
    # ARC-002: recipient_email is no longer accepted/written (kept out of the
    # share flow entirely). The DB column remains but is never populated.


class CreateReferralResponse(BaseModel):
    referral_id: str
    referral_code: str
    share_url: str
    whatsapp_url: Optional[str] = None


class ReferralLookupResponse(BaseModel):
    sender_name: Optional[str]
    intent: Optional[str]
    language: str
    valid: bool


class SentReferralItem(BaseModel):
    referral_code: str
    language: str
    intent: Optional[str]
    channel: str
    recipient_email: Optional[str]
    link_opened: bool
    test_started: bool
    test_completed: bool
    approved: bool
    created_at: str


# ── Account Portal ─────────────────────────────────────────────────────────────

class ChangeSubscriptionRequest(BaseModel):
    new_tier: Literal["free", "learner", "tutor", "intensive"]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


# ── Tutor Portal ───────────────────────────────────────────────────────────────

class UpdatePayoutRequest(BaseModel):
    method: Literal["bank", "mpesa"]
    # Bank fields
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    routing_number: Optional[str] = None
    account_holder_name: Optional[str] = None
    # M-Pesa fields
    mpesa_phone: Optional[str] = None
    mpesa_name: Optional[str] = None
