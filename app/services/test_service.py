import logging
import math
import random
from datetime import datetime, timezone
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

# CEFR levels in ascending order
_CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1"]
_SKILL_AREAS = ["reading", "listening", "vocabulary", "grammar"]

PHASE_1_COUNT = 5
PHASE_2_COUNT = 15

# Never serve more than this many identical interaction modes back to back —
# twenty consecutive multiple-choice questions is what made the old test a
# click-through rather than an assessment.
_MAX_CONSECUTIVE_MODE = 3

# Target share of each interaction mode in a full test, per AUTHORING_GUIDE §2.1.
# The bank is ~60% `choice`, so an unweighted draw produces a test that is ~60%
# multiple choice and reads as one question repeated. Reordering cannot fix that
# — the mix has to be enforced at selection time.
_MODE_TARGET_SHARE = {
    "choice":       0.45,
    "order":        0.10,
    "rank":         0.10,
    "match":        0.10,
    "select_token": 0.10,
    "build":        0.05,
    "bucket":       0.05,
    "multi_choice": 0.05,
}


# Weighted scoring for CEFR determination
_SKILL_WEIGHTS = {
    "grammar": 0.30,
    "reading": 0.30,
    "vocabulary": 0.20,
    "listening": 0.20,
}

_CEFR_THRESHOLDS = [
    (0.80, "C1"),
    (0.65, "B2"),
    (0.50, "B1"),
    (0.35, "A2"),
    (0.0,  "A1"),
]


def _mode_caps(count: int) -> dict[str, int]:
    """Per-mode ceiling for a run of `count` questions (always at least one)."""
    return {mode: max(1, math.ceil(share * count))
            for mode, share in _MODE_TARGET_SHARE.items()}


def _pick_balanced(pool: list[dict], count: int,
                   mode_counts: dict[str, int], caps: dict[str, int]) -> list[dict]:
    """
    Draw `count` questions from `pool`, respecting per-mode ceilings.

    Questions whose mode is already at its cap are held back and used only if
    the pool would otherwise run dry — a thin skill must never shorten the test.
    """
    picked: list[dict] = []
    available = list(pool)
    random.shuffle(available)

    deferred: list[dict] = []
    for q in available:
        if len(picked) >= count:
            break
        mode = q.get("question_type")
        if mode_counts.get(mode, 0) >= caps.get(mode, count):
            deferred.append(q)
            continue
        picked.append(q)
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

    for q in deferred:
        if len(picked) >= count:
            break
        picked.append(q)
        mode = q.get("question_type")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

    return picked


# Exactly what the browser is allowed to see. An allowlist, not a denylist: a new
# reviewer-only column added to app.test_questions must never reach the client
# just because nobody remembered to add it to a blocklist. `correct_answer` is
# the key itself; `explanation` and `distractor_rationale` narrate the answer and
# are reviewer-only by product decision — a rationale reading "A is the literal
# meaning and a strong distractor" leaks the key by implication.
_CLIENT_ROW_FIELDS = (
    "id", "language", "skill_area", "cefr_level", "source_unit", "question_type",
)

# Presentation keys, stored inside the `content` jsonb column but flat on the
# wire. `bank_version` is authoring metadata and stays server-side.
_CLIENT_CONTENT_FIELDS = ("tag", "prompt", "stimulus", "interaction")


def _to_client_shape(questions: list[dict]) -> list[dict]:
    """
    Convert stored rows into the wire shape the renderers expect.

    QUESTION_SCHEMA.md §2 is the contract between the authored bank, this table,
    and `tauka-react-web/src/routes/test-question.jsx` — and it puts `tag`,
    `prompt`, `stimulus` and `interaction` at the TOP level. Storage nests them
    in the `content` jsonb column, so they must be lifted here.

    Returning the raw row instead is not a partial failure, it is a total one:
    the renderer dispatches on `q.interaction.mode`, which is `undefined` for a
    nested row, so EVERY question falls through to "This question type (unknown)
    can't be displayed" — while the preview bank, which is already flat, renders
    perfectly. That divergence is exactly why this survived testing.
    """
    out = []
    for row in questions:
        content = row.get("content") or {}
        q = {k: row.get(k) for k in _CLIENT_ROW_FIELDS}
        q.update({k: content.get(k) for k in _CLIENT_CONTENT_FIELDS})
        out.append(q)
    return out


def is_answer_correct(submitted, correct, mode: str) -> bool:
    """
    Compare a submitted answer against the key for one interaction mode.

    Every mode is closed-set, so every comparison is exact — see
    `QUESTION_SCHEMA.md` §5. Submitted values arrive as {"mode", "value"}; the
    stored key may be the same envelope or a bare value.
    """
    if submitted is None or correct is None:
        return False

    if isinstance(submitted, dict) and "value" in submitted:
        if submitted.get("mode") != mode:
            return False
        submitted = submitted["value"]
    if isinstance(correct, dict) and "value" in correct and "mode" in correct:
        correct = correct["value"]

    # Single id: choice, select_token
    if mode in ("choice", "select_token"):
        return isinstance(submitted, str) and submitted == correct

    # Unordered set: multi_choice
    if mode == "multi_choice":
        if not isinstance(submitted, list) or not isinstance(correct, list):
            return False
        return sorted(map(str, submitted)) == sorted(map(str, correct))

    # Ordered sequence: order, rank, build
    if mode in ("order", "rank", "build"):
        if not isinstance(submitted, list) or not isinstance(correct, list):
            return False
        return list(map(str, submitted)) == list(map(str, correct))

    # Mapping left → right: match
    if mode == "match":
        if not isinstance(submitted, dict) or not isinstance(correct, dict):
            return False
        return {str(k): str(v) for k, v in submitted.items()} == \
               {str(k): str(v) for k, v in correct.items()}

    # Mapping bucket → items; item order within a bucket is not meaningful
    if mode == "bucket":
        if not isinstance(submitted, dict) or not isinstance(correct, dict):
            return False
        if set(submitted) != set(correct):
            return False
        return all(sorted(map(str, submitted[b])) == sorted(map(str, correct[b]))
                   for b in correct)

    logger.warning("Unknown interaction mode during scoring: %s", mode)
    return False


async def create_test_session(
    language: str,
    ip_hash: str,
    user_agent: str,
    referrer_student_id: str | None = None,
    referral_code: str | None = None,
) -> dict:
    """Create a new test session and return Phase 1 questions."""
    # One fetch serves the validation count and both selection passes below.
    bank = _fetch_all_active(language)

    total = len(bank)
    if total < 20:
        raise ValueError(f"Not enough questions for language '{language}' (found {total}, need 20)")

    # Select Phase 1: A1-A2, spread across skill areas
    phase1_questions = _select_phase1_questions(language, pool=bank)
    # Provisionally reserve Phase 2 so question_ids is complete from the start.
    # capture_email re-selects against the real floor once Phase 1 is scored.
    phase2_questions = _select_phase2_questions(
        language, "A1", count=PHASE_2_COUNT,
        exclude_ids={q["id"] for q in phase1_questions},
        pool=bank,
    )

    all_question_ids = [q["id"] for q in phase1_questions + phase2_questions]

    session_result = db.schema("app").table("test_sessions").insert({
        "language": language,
        "status": "phase_1",
        "referrer_student_id": referrer_student_id,
        "referral_code": referral_code,
        "question_ids": all_question_ids,
        "ip_hash": ip_hash,
        "user_agent": user_agent,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    session_id = session_result.data[0]["id"]

    _shuffle_options(phase1_questions)

    return {
        "session_id": session_id,
        "questions": _to_client_shape(phase1_questions),
    }


def _shuffle_options(questions: list[dict]) -> None:
    """
    Randomise option order in place, before the rows are reshaped for the wire.

    Options live at `content.interaction.options` (QUESTION_SCHEMA.md §4). This
    previously read `content.options`, which does not exist on any row, so it
    silently shuffled nothing. Harmless in practice — the client reshuffles
    deterministically per question id in `useDisplayOrder` — but it meant the
    server was not doing what it claimed.
    """
    for q in questions:
        interaction = (q.get("content") or {}).get("interaction")
        if isinstance(interaction, dict):
            opts = interaction.get("options")
            if isinstance(opts, list) and opts:
                random.shuffle(opts)


def _fetch_all_active(language: str) -> list[dict]:
    """
    Every active question for a language, in ONE round trip.

    Selection needs ten different slices of this bank (four skills × two phases,
    plus two backfills). Issuing a query per slice meant ten sequential requests
    to a remote Supabase at ~1.2-1.7s of latency each, so creating a session took
    ~16s and the web app sat on "Preparing your assessment…" long enough to look
    hung. The bank is small — 66 rows / 91 KB for Amharic A1-A2 — so the whole
    thing is cheaper to fetch once and slice in memory than to filter server-side
    ten times. Callers pass the result down through `pool=`.

    Deliberately unbounded, as before: an earlier version capped candidates at 2
    rows (Phase 1) and 5 rows (Phase 2) with no ORDER BY, so Postgres returned
    the same rows every time and the same handful of questions circulated no
    matter how large the bank grew. Randomisation happens in Python, over the
    whole pool.
    """
    return db.schema("app").table("test_questions").select("*").eq(
        "language", language
    ).eq("active", True).execute().data or []


def _filter_pool(pool: list[dict], levels: list[str],
                 skill: str | None = None) -> list[dict]:
    """Slice an already-fetched bank by CEFR level and (optionally) skill."""
    return [q for q in pool
            if q.get("cefr_level") in levels
            and (skill is None or q.get("skill_area") == skill)]


def _interleave_modes(questions: list[dict]) -> list[dict]:
    """
    Reorder so no more than _MAX_CONSECUTIVE_MODE questions share an interaction
    mode.

    Always places the mode with the most questions still outstanding, deferring
    it only when it would exceed the run limit. Spending the scarce modes last
    is what keeps the dominant one (usually `choice`, ~60% of the bank) broken
    up all the way to the end — a reactive greedy exhausts the rare modes early
    and leaves a long uniform tail.

    If one mode is too dominant for any valid arrangement to exist, this
    degrades to the longest runs at the end rather than failing.
    """
    buckets: dict[str, list[dict]] = {}
    for q in questions:
        buckets.setdefault(q.get("question_type"), []).append(q)
    for group in buckets.values():
        random.shuffle(group)

    out: list[dict] = []
    run_mode, run_len = None, 0

    while any(buckets.values()):
        order = sorted((m for m, g in buckets.items() if g),
                       key=lambda m: len(buckets[m]), reverse=True)
        pick_mode = next(
            (m for m in order
             if not (m == run_mode and run_len >= _MAX_CONSECUTIVE_MODE)),
            order[0],          # only the run mode remains — unavoidable
        )
        out.append(buckets[pick_mode].pop())
        run_len = run_len + 1 if pick_mode == run_mode else 1
        run_mode = pick_mode

    return out


def _select_phase1_questions(language: str,
                             pool: list[dict] | None = None) -> list[dict]:
    """Select 5 Phase 1 questions: A1-A2, one per skill area plus a top-up."""
    bank = _fetch_all_active(language) if pool is None else pool

    questions: list[dict] = []
    chosen_ids: set = set()

    caps = _mode_caps(PHASE_1_COUNT)
    mode_counts: dict[str, int] = {}

    for skill in _SKILL_AREAS:
        candidates = [q for q in _filter_pool(bank, ["A1", "A2"], skill)
                      if q["id"] not in chosen_ids]
        for q in _pick_balanced(candidates, 1, mode_counts, caps):
            questions.append(q)
            chosen_ids.add(q["id"])

    if len(questions) < PHASE_1_COUNT:
        candidates = [q for q in _filter_pool(bank, ["A1", "A2"])
                      if q["id"] not in chosen_ids]
        for q in _pick_balanced(candidates, PHASE_1_COUNT - len(questions),
                                mode_counts, caps):
            questions.append(q)
            chosen_ids.add(q["id"])

    return _interleave_modes(questions)[:PHASE_1_COUNT]


def _select_phase2_questions(
    language: str,
    phase_1_floor: str,
    count: int = PHASE_2_COUNT,
    exclude_ids: set | None = None,
    pool: list[dict] | None = None,
) -> list[dict]:
    """
    Select Phase 2 questions with adaptive starting difficulty.

    `exclude_ids` must carry the Phase 1 ids — without it the same question can
    appear twice in one sitting, and `submit_final` then counts it twice,
    inflating that skill's denominator.
    """
    bank = _fetch_all_active(language) if pool is None else pool

    exclude_ids = set(exclude_ids or ())
    floor_idx = _CEFR_LEVELS.index(phase_1_floor) if phase_1_floor in _CEFR_LEVELS else 0
    allowed_levels = _CEFR_LEVELS[floor_idx:]

    # Spread across skills, distributing the remainder rather than truncating:
    # 15 // 4 == 3 gave 12 questions for a test that promises 20.
    base, extra = divmod(count, len(_SKILL_AREAS))
    quotas = {skill: base + (1 if i < extra else 0)
              for i, skill in enumerate(_SKILL_AREAS)}

    questions: list[dict] = []
    chosen_ids = set(exclude_ids)
    caps = _mode_caps(count)
    mode_counts: dict[str, int] = {}

    for skill in _SKILL_AREAS:
        candidates = [q for q in _filter_pool(bank, allowed_levels, skill)
                      if q["id"] not in chosen_ids]
        for q in _pick_balanced(candidates, quotas[skill], mode_counts, caps):
            questions.append(q)
            chosen_ids.add(q["id"])

    # A thin skill must not shrink the test — backfill from any skill.
    if len(questions) < count:
        candidates = [q for q in _filter_pool(bank, allowed_levels)
                      if q["id"] not in chosen_ids]
        for q in _pick_balanced(candidates, count - len(questions), mode_counts, caps):
            questions.append(q)
            chosen_ids.add(q["id"])

    return _interleave_modes(questions)[:count]


async def submit_phase_1(session_id: str, answers: dict) -> dict:
    """Score Phase 1 answers and update session state."""
    result = db.schema("app").table("test_sessions").select("*").eq("id", session_id).execute()
    if not result.data:
        raise ValueError("Session not found")

    session = result.data[0]
    if session["status"] != "phase_1":
        raise ValueError(f"Session is not in phase_1 state (current: {session['status']})")

    phase1_ids = session["question_ids"][:PHASE_1_COUNT]
    q_result = db.schema("app").table("test_questions").select(
        "id, correct_answer, skill_area, cefr_level, question_type"
    ).in_("id", phase1_ids).execute()

    questions = {q["id"]: q for q in q_result.data}

    correct = 0
    skill_results: dict[str, dict] = {s: {"correct": 0, "total": 0} for s in _SKILL_AREAS}

    for qid, answer in answers.items():
        q = questions.get(qid)
        if not q:
            continue
        skill = q["skill_area"]
        skill_results[skill]["total"] += 1
        if is_answer_correct(answer, q["correct_answer"], q.get("question_type")):
            correct += 1
            skill_results[skill]["correct"] += 1

    # Infer floor level from Phase 1 score
    score_pct = correct / max(len(phase1_ids), 1)
    phase_1_floor = "A2" if score_pct >= 0.6 else "A1"

    adaptive_state = {"phase_1_floor": phase_1_floor, "score_pct": score_pct}

    db.schema("app").table("test_sessions").update({
        "status": "phase_1_complete",
        "answers": answers,
        "phase_1_score": {"correct": correct, "total": len(phase1_ids),
                          "breakdown": skill_results},
        "adaptive_state": adaptive_state,
    }).eq("id", session_id).execute()

    return {"correct": correct, "total": len(phase1_ids), "continue": True}


async def capture_email(session_id: str, name: str, email: str) -> dict:
    """Capture email and return Phase 2 questions with adaptive difficulty."""
    result = db.schema("app").table("test_sessions").select("*").eq("id", session_id).execute()
    if not result.data:
        raise ValueError("Session not found")

    session = result.data[0]
    if session["status"] not in ("phase_1_complete",):
        raise ValueError(f"Session not ready for Phase 2 (current: {session['status']})")

    adaptive_state = session.get("adaptive_state", {})
    phase_1_floor = adaptive_state.get("phase_1_floor", "A1")

    # Re-select Phase 2 questions based on adaptive floor, never repeating a
    # question the learner already answered in Phase 1.
    phase1_ids = session["question_ids"][:PHASE_1_COUNT]
    phase2_questions = _select_phase2_questions(
        session["language"], phase_1_floor, count=PHASE_2_COUNT,
        exclude_ids=set(phase1_ids),
    )
    phase2_ids = [q["id"] for q in phase2_questions]

    # Update session: preserve Phase 1 IDs and replace Phase 2 IDs
    new_question_ids = phase1_ids + phase2_ids

    db.schema("app").table("test_sessions").update({
        "status": "phase_2",
        "email": email,
        "name": name,
        "question_ids": new_question_ids,
        "phase_2_started_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", session_id).execute()

    _shuffle_options(phase2_questions)

    return {"questions": _to_client_shape(phase2_questions)}


async def submit_final(session_id: str, answers: dict, background_tasks=None) -> dict:
    """Score all 20 questions and compute final CEFR result."""
    result = db.schema("app").table("test_sessions").select("*").eq("id", session_id).execute()
    if not result.data:
        raise ValueError("Session not found")

    session = result.data[0]
    if session["status"] != "phase_2":
        raise ValueError(f"Session not in phase_2 state (current: {session['status']})")

    all_ids = session["question_ids"]
    q_result = db.schema("app").table("test_questions").select(
        "id, correct_answer, skill_area, cefr_level, question_type"
    ).in_("id", all_ids).execute()
    questions = {q["id"]: q for q in q_result.data}

    # Merge Phase 1 answers with Phase 2 answers
    phase1_answers = session.get("answers", {}) or {}
    all_answers = {**phase1_answers, **answers}

    skill_results: dict[str, dict] = {s: {"correct": 0, "total": 0} for s in _SKILL_AREAS}
    total_correct = 0

    scored: set = set()
    for qid in all_ids:
        q = questions.get(qid)
        if not q or qid in scored:
            continue
        scored.add(qid)
        skill = q["skill_area"]
        skill_results[skill]["total"] += 1
        if is_answer_correct(all_answers.get(qid), q["correct_answer"],
                             q.get("question_type")):
            total_correct += 1
            skill_results[skill]["correct"] += 1

    # Compute CEFR
    cefr_result = compute_cefr_level(skill_results)

    breakdown = {
        skill: {
            "correct": data["correct"],
            "total": data["total"],
            "pct": round(data["correct"] / max(data["total"], 1) * 100),
        }
        for skill, data in skill_results.items()
    }

    description = _generate_description(cefr_result, session["language"], breakdown)
    share_url = f"{settings_web_base_url()}/test/{session['language']}/result/{session_id}"

    final_score = {
        "total_correct": total_correct,
        "total_questions": len(all_ids),
        "breakdown": breakdown,
    }

    db.schema("app").table("test_sessions").update({
        "status": "completed",
        "answers": all_answers,
        "final_score": final_score,
        "cefr_result": cefr_result,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", session_id).execute()

    # Background tasks: send result email, notify referrer
    if background_tasks:
        from app.services.email_service import send_template_email
        email = session.get("email")
        if email:
            background_tasks.add_task(
                send_template_email,
                to=email,
                template_name="test_result",
                template_data={
                    "subject": f"Your Amharic test result: {cefr_result}",
                    "name": session.get("name", ""),
                    "cefr_result": cefr_result,
                    "breakdown": breakdown,
                    "description": description,
                    "share_url": share_url,
                },
            )
        referrer_id = session.get("referrer_student_id")
        if referrer_id:
            background_tasks.add_task(
                _notify_referrer, referrer_id, session.get("name", ""), cefr_result
            )

    return {
        "session_id": session_id,
        "cefr_result": cefr_result,
        "total_correct": total_correct,
        # Count what was actually scored, not what was reserved — a skipped or
        # unmatched question must not silently shrink the denominator.
        "total_questions": len(scored),
        "breakdown": breakdown,
        "description": description,
        "name": session.get("name"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "language": session["language"],
        "share_url": share_url,
        "referrer_student_id": session.get("referrer_student_id"),
    }


def compute_cefr_level(breakdown: dict) -> str:
    """
    Weighted CEFR level from the skill breakdown.

    A skill with no questions is excluded from the weighting rather than scored
    as zero — otherwise a test that happened to serve no listening questions
    would cap the achievable level at B2 no matter how well the learner did.
    """
    weighted = 0.0
    weight_used = 0.0

    for skill, weight in _SKILL_WEIGHTS.items():
        data = breakdown.get(skill) or {}
        if not isinstance(data, dict):
            continue
        total = data.get("total", 0) or 0
        correct = data.get("correct", 0) or 0
        if total > 0:
            weighted += (correct / total) * weight
            weight_used += weight

    if weight_used <= 0:
        return "A1"
    normalised = weighted / weight_used

    for threshold, level in _CEFR_THRESHOLDS:
        if normalised >= threshold:
            return level
    return "A1"


# Human-readable skill names for the result description.
_SKILL_LABELS = {
    "reading": "reading",
    "listening": "listening",
    "vocabulary": "vocabulary",
    "grammar": "grammar",
}


def _generate_description(cefr: str, language: str, breakdown: dict) -> str:
    """
    Describe the result, including the shape of it.

    Reports where the learner is strong and weak — a fact about their score, not
    instruction. Explanations and study advice are deliberately absent: this is
    an assessment, and narrating a fluent speaker's mistakes back at them reads
    as condescending (PLAN.md Phase 2).
    """
    # The language is stored lowercase ("amharic") but reads as a typo in prose.
    language = (language or "").capitalize()

    base = {
        "A1": f"You're just beginning your {language} journey. You recognise a few basic words and phrases.",
        "A2": f"You have foundational {language} skills and can handle simple, everyday topics.",
        "B1": f"You have intermediate {language} skills and can navigate most familiar situations.",
        "B2": f"You have upper-intermediate {language} and can understand complex texts and discussions.",
        "C1": f"You have advanced {language} proficiency and communicate fluently on a wide range of topics.",
    }.get(cefr, f"Your {language} level is {cefr}.")

    scored = {
        skill: data for skill, data in (breakdown or {}).items()
        if isinstance(data, dict) and (data.get("total") or 0) > 0
    }
    if len(scored) < 2:
        return base

    ranked = sorted(scored.items(), key=lambda kv: kv[1].get("pct", 0), reverse=True)
    (top, top_data), (bottom, bottom_data) = ranked[0], ranked[-1]
    spread = top_data.get("pct", 0) - bottom_data.get("pct", 0)

    # Only call out a difference that is actually a difference. On a 20-question
    # test a handful of points is one question, not a pattern.
    if spread < 20:
        return f"{base} Your results were even across all four skills."

    return (f"{base} Your strongest area was {_SKILL_LABELS.get(top, top)} "
            f"({top_data.get('pct', 0)}%), and your weakest was "
            f"{_SKILL_LABELS.get(bottom, bottom)} ({bottom_data.get('pct', 0)}%).")


async def _notify_referrer(referrer_id: str, tester_name: str, cefr_result: str) -> None:
    try:
        user_result = db.auth.admin.get_user_by_id(referrer_id)
        email = user_result.user.email if user_result and user_result.user else None
        if email:
            from app.services.email_service import send_email
            await send_email(
                to=email,
                subject=f"{tester_name} just took the {cefr_result} language test",
                html_body=f"<p>Someone you referred just completed the language proficiency test and scored <strong>{cefr_result}</strong>.</p>",
            )
    except Exception as exc:
        logger.warning("Failed to notify referrer %s: %s", referrer_id, exc)


def settings_web_base_url() -> str:
    from app.config import settings
    return settings.web_base_url
