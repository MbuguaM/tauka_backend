import json
import logging
from datetime import datetime, timezone
from app.config import settings
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# ── Payout settings encryption ────────────────────────────────────────────────

def _get_fernet():
    """Return a Fernet instance using the configured encryption key."""
    from cryptography.fernet import Fernet
    key = settings.encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(data: dict) -> str:
    f = _get_fernet()
    return f.encrypt(json.dumps(data).encode()).decode()


def _decrypt(token: str) -> dict:
    f = _get_fernet()
    return json.loads(f.decrypt(token.encode()).decode())


# ── Earnings ──────────────────────────────────────────────────────────────────

async def get_earnings(tutor_id: str, period: str = "current_month") -> dict:
    """Aggregate session earnings for the tutor for a given period."""
    now = datetime.now(timezone.utc)

    if period == "current_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = now.strftime("%B %Y")
    elif period == "last_month":
        first_of_month = now.replace(day=1)
        import calendar
        last_month = first_of_month.replace(month=first_of_month.month - 1 if first_of_month.month > 1 else 12)
        if first_of_month.month == 1:
            last_month = last_month.replace(year=last_month.year - 1)
        start = last_month.replace(hour=0, minute=0, second=0, microsecond=0)
        label = last_month.strftime("%B %Y")
    else:
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        label = "All time"

    # §77: `app.tutor_sessions` was DROPPED — it never had a writer, so every
    # earnings figure computed from it was a confident zero. The two real
    # sources are the ones the payout run uses, so the number a tutor sees here
    # and the number they are paid cannot disagree:
    #   * app.cohort_session.lead_tutor_id — who LED the occurrence (the rota
    #     may hand it to a guest rather than the cohort's own tutor);
    #   * app.one_to_one_session.tutor_id  — the tutor the student booked.
    # `completed` only, both of them: a past session nobody marked is not a
    # session that happened (§77.1).
    sessions = []
    try:
        cohort = db.schema("app").table("cohort_session").select(
            "id, scheduled_at, duration_minutes"
        ).eq("lead_tutor_id", tutor_id).eq("status", "completed").gte(
            "scheduled_at", start.isoformat()).execute()
        sessions.extend(cohort.data or [])
    except Exception as exc:
        logger.warning("cohort session count failed for %s: %s", tutor_id, exc)
    try:
        one = db.schema("app").table("one_to_one_session").select(
            "id, scheduled_at, duration_minutes"
        ).eq("tutor_id", tutor_id).eq("status", "completed").gte(
            "scheduled_at", start.isoformat()).execute()
        sessions.extend(one.data or [])
    except Exception as exc:
        logger.warning("one-to-one session count failed for %s: %s", tutor_id, exc)

    # Get per-session rate from tutor profile
    try:
        profile_result = db.schema("app").table("tutor").select(
            "per_session_rate_cents"
        ).eq("id", tutor_id).execute()
        per_session_rate_cents = (
            profile_result.data[0].get("per_session_rate_cents", 0)
            if profile_result.data else 0
        )
    except Exception:
        per_session_rate_cents = 0

    # §75: the rate is no longer a static column. `tutor.per_session_rate_cents`
    # is ORPHANED — held at 0 for every tutor and written by nothing — so an
    # earnings figure computed from it is a confident zero.
    #
    # The rate now lives in `app.tutor_rate_version`: base × tenure × quality,
    # against a rate card for the tutor's country, carrying its own currency.
    # `per_session_rate_cents` stays as the fallback ONLY so a tutor whose rate
    # has not been computed yet sees the old number rather than a crash.
    currency = "USD"
    try:
        rate_result = db.schema("app").table("tutor_rate_version").select(
            "computed_rate_minor, currency"
        ).eq("tutor_id", tutor_id).is_("effective_to", "null").limit(1).execute()
        if rate_result.data:
            per_session_rate_cents = int(rate_result.data[0]["computed_rate_minor"])
            currency = rate_result.data[0]["currency"]
    except Exception:
        pass

    # PY-003: all money math stays in integer cents. The 20% platform fee is
    # applied as an integer operation; values are converted to a rounded
    # decimal only at the serialization step below for display.
    per_session_rate_cents = int(per_session_rate_cents or 0)
    PLATFORM_FEE_PCT = 20  # 20% platform fee (integer percent)
    sessions_completed = len(sessions)
    gross_earnings_cents = per_session_rate_cents * sessions_completed
    net_earnings_cents = gross_earnings_cents * (100 - PLATFORM_FEE_PCT) // 100

    breakdown = [
        {
            "session_id": s.get("id"),
            "date": s.get("started_at"),
            "duration": s.get("duration_minutes"),
            "students": s.get("student_count", 1),
            "type": s.get("session_type", "cohort"),
            "amount_cents": per_session_rate_cents,
            "amount": round(per_session_rate_cents / 100, 2),
        }
        for s in sessions
    ]

    return {
        "period": label,
        "sessions_completed": sessions_completed,
        "gross_earnings_cents": gross_earnings_cents,
        "net_earnings_cents": net_earnings_cents,
        "gross_earnings": round(gross_earnings_cents / 100, 2),
        "net_earnings": round(net_earnings_cents / 100, 2),
        "per_session_rate_cents": per_session_rate_cents,
        "per_session_rate": round(per_session_rate_cents / 100, 2),
        # NOT necessarily USD any more (D-17: a rate card per country). The
        # `_cents` keys above are kept for the existing web client and are
        # minor units of THIS currency, not of a dollar.
        "currency": currency,
        "payout_status": "pending",
        "payout_date": None,
        "breakdown": breakdown,
    }


async def get_earnings_history(tutor_id: str, limit: int = 12, offset: int = 0) -> list[dict]:
    """Return monthly earnings history for the tutor."""
    # §77: this called an RPC `get_tutor_monthly_earnings` that aggregated
    # `app.tutor_sessions` — a table with no writer, reached through a function
    # that does not exist in this database. It returned [] on every call, from
    # the except below, and looked like "no history yet".
    #
    # The real history is `app.payout_line`: one row per tutor per computed run,
    # carrying the currency and the referral share. A tutor sees what they were
    # actually owed, not a recomputation that could disagree with it.
    try:
        result = db.schema("app").table("payout_line").select(
            "id, currency, amount_minor, session_count, referral_share_minor, "
            "status, settled_at, created_at, "
            "payout_run(period_start, period_end)"
        ).eq("tutor_id", tutor_id).order("created_at", desc=True).range(
            offset, offset + limit - 1).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("get_earnings_history failed: %s", exc)
        return []


# ── Payout settings ───────────────────────────────────────────────────────────

async def get_payout_settings(tutor_id: str) -> dict | None:
    """Return masked payout settings (last4 only — never decrypts for display)."""
    result = db.schema("app").table("tutor_payout_settings").select(
        "method, details_last4"
    ).eq("tutor_id", tutor_id).execute()

    if not result.data:
        return None

    row = result.data[0]
    return {
        "method": row["method"],
        "details": {"last4": row.get("details_last4")},
    }


async def update_payout_settings(tutor_id: str, method: str, raw_details: dict) -> dict:
    """Encrypt and store payout settings."""
    _validate_payout_details(method, raw_details)

    # Determine last4 for display
    if method == "bank":
        last4 = (raw_details.get("account_number") or "")[-4:]
    else:
        last4 = (raw_details.get("mpesa_phone") or "")[-4:]

    encrypted = _encrypt(raw_details)

    db.schema("app").table("tutor_payout_settings").upsert({
        "tutor_id": tutor_id,
        "method": method,
        "encrypted_details": encrypted,
        "details_last4": last4,
        "verified": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="tutor_id").execute()

    return {"method": method, "last4": last4, "verified": False}


def _validate_payout_details(method: str, details: dict) -> None:
    if method == "bank":
        required = ("bank_name", "account_number", "routing_number", "account_holder_name")
        missing = [f for f in required if not details.get(f)]
        if missing:
            raise ValueError(f"Missing bank fields: {missing}")
    elif method == "mpesa":
        if not details.get("mpesa_phone"):
            raise ValueError("mpesa_phone is required")
        if not details.get("mpesa_name"):
            raise ValueError("mpesa_name is required")
    else:
        raise ValueError(f"Unknown payout method: {method}")


# ── Assigned students ─────────────────────────────────────────────────────────

async def get_assigned_students(tutor_id: str) -> list[dict]:
    """Return students assigned to this tutor with aggregated stats."""
    # §77 REGRESSION FIX. This filtered `app.student.tutor_id` and
    # `tutor_ended_at` — both dropped by §73, which replaced the single-tutor
    # column with `app.student_tutor_link`. Since then the query has failed on a
    # missing column and the except below has turned that into an EMPTY ROSTER:
    # every tutor saw "no students", with nothing in the log a caller would see.
    # That is the swallowed-exception failure mode this codebase keeps meeting —
    # "failed" and "nothing to do" rendered identically.
    links = []
    try:
        link_result = db.schema("app").table("student_tutor_link").select(
            "student_id, role, started_at"
        ).eq("tutor_id", tutor_id).eq("state", "active").execute()
        links = link_result.data or []
    except Exception as exc:
        logger.warning("get_assigned_students failed: %s", exc)
        return []

    students = []
    for a in links:
        student_id = a["student_id"]
        try:
            profile_result = db.schema("app").table("student").select(
                "assessed_level, lessons_completed, language"
            ).eq("id", student_id).execute()
            profile = profile_result.data[0] if profile_result.data else {}

            user_result = db.auth.admin.get_user_by_id(student_id)
            name = (user_result.user.user_metadata or {}).get("full_name", "Student") if user_result and user_result.user else "Student"

            # Last session. §77: was `app.tutor_sessions`, dropped. A 1-on-1 is
            # the only per-student session this can answer from — a cohort
            # occurrence is delivered to the whole cohort, so "their last session
            # with me" is not a property of it.
            last_session = None
            try:
                sess_result = db.schema("app").table("one_to_one_session").select(
                    "scheduled_at"
                ).eq("tutor_id", tutor_id).eq("student_id", student_id).eq(
                    "status", "completed").order(
                    "scheduled_at", desc=True).limit(1).execute()
                if sess_result.data:
                    last_session = sess_result.data[0]["scheduled_at"]
            except Exception:
                pass

            students.append({
                "student_id": student_id,
                "name": name,
                # From the student profile, not the link: student_tutor_link
                # carries the relationship, not the learner's attributes.
                "language": profile.get("language"),
                "role": a.get("role"),
                "since": a.get("started_at"),
                "current_level": profile.get("assessed_level"),
                "lessons_completed": profile.get("lessons_completed", 0),
                "last_session_date": last_session,
                "attendance_rate": None,
            })
        except Exception as exc:
            logger.warning("Student fetch failed for %s: %s", student_id, exc)

    return students


async def get_student_detail(tutor_id: str, student_id: str) -> dict:
    """Return detailed profile for one assigned student."""
    # Validate assignment
    # §77 REGRESSION FIX — same cause as get_assigned_students. This filtered
    # columns §73 dropped, so the query raised, the except re-raised as
    # "Assignment check failed", and the route turned that into a 404: a tutor
    # was told "not your student" for every student they have.
    try:
        check = db.schema("app").table("student_tutor_link").select("id").eq(
            "tutor_id", tutor_id
        ).eq("student_id", student_id).eq("state", "active").execute()
        if not check.data:
            raise ValueError("Student not assigned to this tutor")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Assignment check failed: {exc}") from exc

    try:
        profile_result = db.schema("app").table("student").select("*").eq(
            "id", student_id
        ).execute()
        profile = profile_result.data[0] if profile_result.data else {}
    except Exception:
        profile = {}

    try:
        user_result = db.auth.admin.get_user_by_id(student_id)
        meta = user_result.user.user_metadata or {} if user_result and user_result.user else {}
        name = meta.get("full_name", "Student")
        goal = meta.get("learning_goal", "")
    except Exception:
        name, goal = "Student", ""

    return {
        "name": name,
        "language": None,
        "goal": goal,
        "current_level": profile.get("assessed_level"),
        "lessons_completed": profile.get("lessons_completed", 0),
        "total_lessons": None,
        "session_history": [],
        "progress_over_time": [],
    }
