from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Literal, Optional, Any

from app.services.ai_prompts import (
    FRAMED_MODES,
    MAX_CONTEXT,
    MAX_PASSAGE,
    MAX_QUOTE,
)


# ── Existing models ────────────────────────────────────────────────────────────

class AIRequest(BaseModel):
    prompt: str
    provider: Literal["openai", "deepseek", "gemini"] = "deepseek"
    mode: Literal[
        "chat",
        "translation",
        "image_translation",
        # Framed modes: the server owns the instructions and fences the content.
        # `prompt` carries the primary MATERIAL — the highlighted passage, or
        # the chat message — not a prompt. See app/services/ai_prompts.py.
        "lesson_explain",
        "analyse_amharic",
    ] = "chat"
    target_language: Optional[str] = None
    image_url: Optional[str] = None

    # Framed modes only; ignored by the others.
    passage: Optional[str] = Field(None, max_length=MAX_PASSAGE)
    context: Optional[str] = Field(None, max_length=MAX_CONTEXT)

    @model_validator(mode="after")
    def _cap_framed_material(self):
        """
        Bound the material on framed modes.

        `prompt` is deliberately uncapped for `chat` — that path predates this
        and capping it now would reject requests that work today. On the framed
        modes it is content rather than a prompt, so it gets the same treatment
        as the other two fields: a cap is both an injection surface reduction
        and the thing that stops one request spending a whole day's token
        budget.
        """
        if self.mode in FRAMED_MODES and len(self.prompt) > MAX_QUOTE:
            raise ValueError(
                f"prompt must be at most {MAX_QUOTE} characters in {self.mode} mode"
            )
        return self


class MessageRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1, max_length=4000)


class CallRequest(BaseModel):
    room: str
    vendor: Optional[Literal["daily", "livekit"]] = None


# ── Segment A — Subscriptions ──────────────────────────────────────────────────

class SubscriptionCheckoutRequest(BaseModel):
    tier: Literal["learner_tier", "tutor_tier", "intensive_tier"]
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
    new_tier: Literal["free_tier", "learner_tier", "tutor_tier", "intensive_tier"]
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


# ── Tutor referral attribution (plan Phase 1, spec §1.1) ──────────────────────
#
# Distinct from the Segment C.5 models above: those describe a placement-test
# share; these describe a paying signup and the revenue share that follows it.
# CONFLICT-039.

class TutorReferralLinkResponse(BaseModel):
    link_id: str
    code: str
    share_url: str
    created_at: Optional[str] = None
    rotated: bool = False


class TutorReferralLookupResponse(BaseModel):
    valid: bool
    tutor_name: Optional[str] = None


class ClaimReferralRequest(BaseModel):
    # Matches the CHECK on app.tutor_referral_link.code (4..64).
    code: str = Field(..., min_length=4, max_length=64)
    # A slug from tutor_referral_service.SOURCE_PLATFORMS, never free text and
    # never anything the visitor typed. An unrecognised value is dropped, not
    # stored — see normalise_source_platform.
    source_platform: Optional[str] = Field(None, max_length=32)


class ClaimReferralResponse(BaseModel):
    attributed: bool
    already_attributed: bool
    tutor_name: Optional[str] = None
    # attributed | already_attributed | unknown_code | self_referral |
    # no_student_row
    reason: str


class TutorReferralSignupItem(BaseModel):
    """One attributed signup, carrying **no student identity** — see
    tutor_referral_service.get_referral_report for why."""
    signed_up_at: str
    source_platform: Optional[str] = None
    still_active: bool
    first_match_honoured: bool
    vested_share_bp: int


class TutorReferralReportResponse(BaseModel):
    code: Optional[str] = None
    share_url: Optional[str] = None
    total_signups: int
    still_active: int
    first_match_honoured: int
    total_vested_share_bp: int
    # False until Phase 3 computes vesting. The client must render "not yet in
    # effect" rather than a confident 0%.
    vesting_active: bool
    signups: list[TutorReferralSignupItem]


# ── Home/guest relation and the 1-on-1 lane (plan Phase 2) ────────────────────

class CreateLinkRequest(BaseModel):
    student_id: str
    tutor_id: str
    role: Literal["home", "guest"]
    # `referral` is written by the matcher when a referral_signup row decided the
    # home tutor; `handoff` by the §1.7 warm-handoff flow. An admin screen sends
    # `admin`, which is the default and the only one a human picks.
    source: Literal["referral", "matched", "admin", "handoff"] = "admin"


class BookSessionRequest(BaseModel):
    tutor_id: str
    # ISO-8601 instant. Sent back verbatim from the slot the client was shown,
    # rather than reconstructed — the server expanded the availability window and
    # is the only side that did the timezone arithmetic.
    starts_at: str
    # {25, 50} from the pricing page. Never a hardcoded 50.
    duration_minutes: Literal[25, 50] = 50
    availability_id: Optional[str] = None


class CancelSessionRequest(BaseModel):
    session_id: str
    reason: Optional[str] = Field(None, max_length=500)


class MarkSessionRequest(BaseModel):
    """The tutor's record of whether a booked 1-on-1 happened.

    No `pending` here, unlike cohort attendance: a booked session is already the
    unmarked state, so a third value would be a second way to say the same thing.
    """
    session_id: str
    outcome: Literal["completed", "no_show"]


# ── Payouts (plan Phase 3a, spec §1.4) ────────────────────────────────────────

class ApproveRunRequest(BaseModel):
    run_id: str


class RecordDisbursementRequest(BaseModel):
    """Recording a payment a human already made by hand (§11.4's manual driver)."""
    line_id: str
    # An M-Pesa receipt, a bank reference — whatever proves it was sent. A future
    # automated driver writes the same field, so nothing downstream changes.
    external_ref: str = Field(..., min_length=1, max_length=200)
    # Only when the money settled in a currency other than the line's. The table
    # REFUSES that combination without fx_rate and fx_source: otherwise a tutor's
    # real pay drifts with the market and neither side can reconstruct why last
    # month differed from this one (§11.3).
    settled_amount_minor: Optional[int] = Field(None, ge=0)
    settled_currency: Optional[str] = Field(None, min_length=3, max_length=3)
    fx_rate: Optional[float] = Field(None, gt=0)
    fx_source: Optional[str] = Field(None, max_length=100)


# ── Termly commitment (plan Phase 4, spec §1.3) ───────────────────────────────

class CommitmentCheckoutRequest(BaseModel):
    course_id: str
    billing_mode: Literal["prepay", "installment"]
    # The tier the commitment ENTITLES. Not a tier value of its own — §4.4:
    # "app.student.tier stays the entitlement axis; the commitment is a separate
    # contract that the matcher reads as a hard constraint."
    tier: Literal["learner_tier", "tutor_tier", "intensive_tier"] = "tutor_tier"
    # The guarantee the product is sold on (§2.3). Optional at checkout: a
    # student may commit before they have been matched.
    locked_home_tutor_id: Optional[str] = None
    amount_minor: int = Field(..., ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    # e.g. "unit_order:15". A threshold on unit.unit_order, NOT a level name —
    # app.unit has no `level` column, so a level target would never fire.
    milestone_target: Optional[str] = Field(None, max_length=64)
    customer_email: Optional[EmailStr] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CancelCommitmentRequest(BaseModel):
    commitment_id: str
    reason: Optional[str] = Field(None, max_length=500)


# ── Trust (plan Phase 5a, D-12) ───────────────────────────────────────────────

class TrustWarningRequest(BaseModel):
    """An anonymous counter tick.

    **There is no user, conversation or message field here, on purpose** (§17.3).
    The caller is authenticated so the counter cannot be spammed; nothing about
    them is recorded. Adding an id to this model would turn a counter into
    surveillance and re-open L-008.
    """
    pattern_class: str = Field(..., min_length=1, max_length=40)
    proceeded: bool = False
