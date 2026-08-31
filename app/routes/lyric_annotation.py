"""POST /ai/lyrics/annotate — annotate one song's lyrics.

Plan: `explore-lyric-annotation-plan.md` §5.

**Why not /ai/generate**, three reasons each independently disqualifying:

1. Its rate limit is sized for a chat turn — `deepseek` is 8,000 tokens/day per
   user. One song plus the course contents exceeds a tutor's entire daily
   budget in a single request.
2. It bills the caller. Content production charged to a tutor's personal ledger
   makes the stats page describe something that is not happening.
3. It is single-shot, with no overflow path.

**This endpoint does not write the lyric file.** It takes lines and returns
lines. `ExploreUploadService.uploadLyricFile` is the single writer, under a
tutor/admin Storage policy, and the content-addressed path is the cache
invalidation. Python stays a pure transformer and adds no new writer to a
tauka-flutter-owned surface. It never sees a video id or a Storage path, and it
has no idea which platform the text came from — if it ever needs to know,
something has gone wrong.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.services.ai_prompts import ANNOTATION_PROMPT_VERSION, build_lyric_annotate
from app.services.ai_services import call_deepseek_annotate, take_last_usage
from app.services.ai_usage_service import log_ai_usage
from app.services.lyric_annotation_service import (
    parse_response,
    split_for_retry,
    validate,
)
from app.services.rate_limit_service import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_LINES = 200
MAX_LINE_CHARS = 400


class AnnotateRequest(BaseModel):
    lines: list[str] = Field(..., min_length=1)
    song_title: str = ""
    # Flattened unit/note titles, built by the client from the cached TOC. Sent
    # rather than read here because the course document is tauka-flutter's, and
    # this service has no business reaching into its Storage.
    course_contents: str = ""
    language: str = "amharic"


class AnnotateResponse(BaseModel):
    lines: list[dict]
    failed_lines: list[int] = []
    prompt_version: str
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    problems: list[str] = []


async def _annotate_block(
    lines: list[str],
    song_title: str,
    toc: str,
    user_id: str,
    depth: int = 0,
) -> tuple[list[dict], list[str]]:
    """One request, splitting on overflow. Returns ``(entries, problems)``.

    On any failure the caller still gets whatever landed: a half-annotated song
    is a useful artefact — the wizard shows what came back and offers to retry
    the gaps — where returning nothing throws away work already paid for.
    """
    system, user = build_lyric_annotate(lines, song_title, toc)
    try:
        content, finish = await call_deepseek_annotate(system, user)
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        logger.warning("lyric annotate call failed: %s", exc)
        return [], [f"provider error: {exc}"]

    await _record_usage(user_id, len(lines))

    truncated = finish == "length"
    if not truncated:
        try:
            entries = parse_response(content)
        except ValueError as exc:
            return [], [f"unparseable: {exc}"]
        result = validate(lines, entries)
        if result.ok:
            return result.lines, []
        # One structural failure is worth a split before it is worth a repair
        # prompt: a short answer is nearly always a long song.
        if len(result.lines) or depth >= 3:
            return result.lines, result.problems
        truncated = True

    if depth >= 3 or len(lines) <= 4:
        return [], ["response truncated and cannot be split further"]

    out: list[dict] = []
    problems: list[str] = []
    for part in split_for_retry(lines):
        got, probs = await _annotate_block(
            part, song_title, toc, user_id, depth + 1)
        out.extend(got)
        problems.extend(probs)
    return out, problems


async def _record_usage(user_id: str, n_lines: int) -> None:
    """Write the ledger row.

    `app.ai_usage` has never been written to, so these are the first rows it
    will ever hold — which matters more than it sounds. The stats page reads
    this table, and content-production tokens landing in it undifferentiated
    would make every per-student number it shows include work no student did.
    `mode='lyric_annotate'` is what lets that page exclude them explicitly.
    """
    usage = take_last_usage()
    try:
        await log_ai_usage(
            user_id=user_id,
            provider="deepseek",
            model="deepseek-chat",
            mode="lyric_annotate",
            usage=usage,
            # A rough floor when the provider reported nothing; log_ai_usage
            # records which of the two it was, so the stats page never passes an
            # estimate off as a measurement.
            estimated_total=n_lines * 120,
        )
    except Exception as exc:                       # noqa: BLE001
        # A ledger failure must not lose the annotation the tutor just paid for.
        logger.warning("ai_usage write failed for lyric_annotate: %s", exc)


@router.post("/lyrics/annotate", response_model=AnnotateResponse)
async def annotate_lyrics(
    req: AnnotateRequest,
    user_id: str = Depends(get_current_user),
):
    if len(req.lines) > MAX_LINES:
        raise HTTPException(413, f"at most {MAX_LINES} lines per request")
    if any(len(line) > MAX_LINE_CHARS for line in req.lines):
        raise HTTPException(413, "a line exceeds the length cap")

    # Its own namespace, keyed by the tutor. The `deepseek` plan is the student
    # chat budget and stays where it is — raising it to fit content production
    # would quietly hand every student a bigger allowance.
    allowed = check_rate_limit(
        user_id,
        tokens=len(" ".join(req.lines)) // 3,
        provider="lyric_annotation",
        mode="lyric_annotate",
    )
    if not allowed:
        raise HTTPException(429, "lyric annotation budget exhausted for today")

    entries, problems = await _annotate_block(
        req.lines, req.song_title, req.course_contents, user_id)

    annotated = {e.get("original_phrase") for e in entries}
    failed = [i for i, line in enumerate(req.lines) if line not in annotated]

    return AnnotateResponse(
        lines=entries,
        failed_lines=failed,
        prompt_version=ANNOTATION_PROMPT_VERSION,
        problems=problems,
    )
