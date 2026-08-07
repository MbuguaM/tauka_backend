import json
import logging
from app.services.supabase_client import supabase_admin as db
from app.services.ai_services import call_deepseek_chat

logger = logging.getLogger(__name__)

_GENERATION_PROMPT = """You are creating test questions for a {language} language proficiency assessment.

Source material (FSI Lesson {lesson_number}, CEFR level {cefr_level}):
{lesson_content}

Generate {count} test questions. For each, output a JSON object with these fields:
{{
  "question_type": "multiple_choice"|"reorder"|"fill_conjugation"|"register_id"|"reading_comp"|"idiom",
  "skill_area": "reading"|"listening"|"vocabulary"|"grammar",
  "cefr_level": "{cefr_level}",
  "content": {{ ... }},
  "correct_answer": "...",
  "explanation": "..."
}}

Requirements:
- Wrong options must be plausible (real {language} words at similar level)
- Only one unambiguously correct answer
- Difficulty must match stated CEFR level
- No knowledge beyond the source lesson scope

Output ONLY a JSON array of question objects, no other text.
"""

_VALIDATION_PROMPT = """You are validating language test questions for quality and accuracy.

Review each question below and for each return:
{{
  "index": <0-based index>,
  "valid": true|false,
  "issues": ["issue 1", "issue 2"] or []
}}

Questions:
{questions_json}

Output ONLY a JSON array of validation results, no other text.
"""


async def generate_questions_from_fsi(
    language: str,
    lesson_number: int,
    cefr_level: str,
    count: int = 5,
) -> list[dict]:
    """Generate test questions from FSI lesson content using DeepSeek."""
    # Fetch lesson content (table is not yet in the schema; falls back to a stub)
    lesson_content = ""
    try:
        lesson_result = db.schema("app").table("fsi_lessons").select("content").eq(
            "language", language
        ).eq("lesson_number", lesson_number).execute()
        if lesson_result.data:
            lesson_content = lesson_result.data[0].get("content", "")
    except Exception:
        pass

    if not lesson_content:
        lesson_content = f"FSI {language} Lesson {lesson_number} — vocabulary and grammar for {cefr_level} level"

    prompt = _GENERATION_PROMPT.format(
        language=language,
        lesson_number=lesson_number,
        lesson_content=lesson_content,
        cefr_level=cefr_level,
        count=count,
    )

    try:
        raw = await call_deepseek_chat(prompt)
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        questions = json.loads(raw)
        for q in questions:
            q["language"] = language
            q["fsi_lesson_ref"] = f"{language.lower()}_lesson_{lesson_number}"
            q["ai_generated"] = True
            q["human_reviewed"] = False
            q["active"] = False  # inactive until validated
        return questions
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Question generation failed (lesson %s): %s", lesson_number, exc)
        return []


async def validate_questions(questions: list[dict]) -> list[dict]:
    """Send questions through DeepSeek for quality validation."""
    if not questions:
        return []

    questions_json = json.dumps(questions, ensure_ascii=False, indent=2)
    prompt = _VALIDATION_PROMPT.format(questions_json=questions_json)

    try:
        raw = await call_deepseek_chat(prompt)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        validations = json.loads(raw)

        result = []
        for i, q in enumerate(questions):
            v = next((v for v in validations if v.get("index") == i), None)
            q["valid"] = v.get("valid", False) if v else False
            q["validation_issues"] = v.get("issues", []) if v else ["Validation failed"]
            result.append(q)
        return result
    except Exception as exc:
        logger.error("Question validation failed: %s", exc)
        for q in questions:
            q["valid"] = False
            q["validation_issues"] = ["Validation failed"]
        return questions


async def seed_question_bank(
    language: str,
    lesson_range: tuple[int, int] | None = None,
) -> dict:
    """Full pipeline: generate → validate → insert questions."""
    from app.services.test_service import _CEFR_LEVELS

    start, end = lesson_range or (1, 60)
    generated_total = 0
    valid_total = 0
    inserted_total = 0

    # Map lesson ranges to CEFR levels
    lessons_per_level = max(1, (end - start + 1) // len(_CEFR_LEVELS))

    for i, cefr_level in enumerate(_CEFR_LEVELS):
        level_start = start + i * lessons_per_level
        level_end = min(level_start + lessons_per_level - 1, end)

        for lesson_num in range(level_start, level_end + 1):
            questions = await generate_questions_from_fsi(
                language, lesson_num, cefr_level, count=5
            )
            generated_total += len(questions)

            if not questions:
                continue

            validated = await validate_questions(questions)
            valid_questions = [q for q in validated if q.get("valid")]
            valid_total += len(valid_questions)

            rows = [to_db_row(q, language) for q in valid_questions]

            if rows:
                db.schema("app").table("test_questions").upsert(
                    rows, on_conflict="id"
                ).execute()
                inserted_total += len(rows)

    return {
        "generated": generated_total,
        "valid": valid_total,
        "inserted": inserted_total,
    }


# Columns that actually exist on app.test_questions. Anything else the generator
# emits belongs inside `content`. Previously the raw model output was inserted
# verbatim, which failed on every run: `explanation` had no column, and the
# NOT NULL `language` was never set — so the bank has never loaded.
_DB_COLUMNS = {
    "id", "language", "cefr_level", "skill_area", "question_type", "content",
    "correct_answer", "source_unit", "explanation", "distractor_rationale",
    "audio_url", "image_url", "fsi_lesson_ref", "ai_generated",
    "human_reviewed", "active", "flag_count",
}

# Keys of the authored question that live inside the `content` jsonb.
_CONTENT_KEYS = ("tag", "prompt", "stimulus", "interaction", "hint")


def to_db_row(question: dict, language: str) -> dict:
    """
    Map an authored v4 question onto the app.test_questions columns.

    Contract: Course creator/Amharic_website_test/QUESTION_SCHEMA.md §6.
    `answer` becomes `correct_answer` and `interaction.mode` becomes
    `question_type`; presentation goes into `content`.
    """
    q = dict(question)
    for field in ("valid", "validation_issues"):
        q.pop(field, None)

    interaction = q.get("interaction") or {}
    answer = q.get("answer")
    # Store the bare value; the comparator accepts either form, and the bare
    # value keeps `correct_answer` readable in the dashboard.
    if isinstance(answer, dict) and "value" in answer:
        answer = answer["value"]

    row = {
        "id": q.get("id"),
        "language": q.get("language") or language,
        "cefr_level": q.get("cefr_level"),
        "skill_area": q.get("skill_area"),
        "question_type": interaction.get("mode") or q.get("question_type"),
        "content": {k: q[k] for k in _CONTENT_KEYS if k in q},
        "correct_answer": answer,
        "source_unit": q.get("source_unit"),
        "explanation": q.get("explanation"),
        "distractor_rationale": q.get("distractor_rationale"),
        "active": q.get("active", True),
        "ai_generated": q.get("ai_generated", True),
        "human_reviewed": q.get("human_reviewed", False),
    }

    stimulus = q.get("stimulus") or {}
    if stimulus.get("kind") == "image" and stimulus.get("src"):
        row["image_url"] = stimulus["src"]

    return {k: v for k, v in row.items() if k in _DB_COLUMNS}


async def load_question_bank(path: str, language: str = "amharic") -> dict:
    """
    Load an authored bank file straight into app.test_questions.

    This is how the v4 bank ships — the AI generation path above is for creating
    new drafts, not for loading a reviewed bank. Upserts on id, so re-running
    after an edit updates in place rather than duplicating.
    """
    import json
    from pathlib import Path

    questions = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(questions, dict):
        questions = questions.get("questions") or []

    rows, skipped = [], []
    for q in questions:
        row = to_db_row(q, language)
        if not row.get("id") or not row.get("question_type") or row.get("correct_answer") is None:
            skipped.append(q.get("id") or "<no id>")
            continue
        rows.append(row)

    inserted = 0
    for i in range(0, len(rows), 100):          # chunked: one 150-row request can time out
        batch = rows[i:i + 100]
        db.schema("app").table("test_questions").upsert(batch, on_conflict="id").execute()
        inserted += len(batch)

    if skipped:
        logger.warning("Skipped %d malformed question(s): %s", len(skipped), skipped)

    return {"total": len(questions), "loaded": inserted, "skipped": skipped}
