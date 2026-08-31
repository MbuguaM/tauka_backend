"""Lyric annotation — chunking, validation, merge.

Plan: `explore-lyric-annotation-plan.md` §5, §6, §7.1.

Everything here is a pure function over data. No network, no Supabase, no
FastAPI — so the whole thing is testable offline against the 22 real lyric files
in `Course creator/explore_items_normalised/`, which is what the plan's phase 8
asks for.

Two decisions from the plan are enforced here rather than trusted:

* **One request per song, split only on overflow.** The corpus was generated
  whole-song and reads coherently, so chunking is not a quality mechanism; it is
  what happens when a response runs out of room. Splitting everything by default
  would pay the system-prompt cost N times over and cut couplets in half.
* **Fill empty only.** A re-run must never overwrite a line a human corrected.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# The whole point of the split path is that it fires rarely. The longest song in
# the catalogue is 80 lines and fits comfortably; this is a floor to stop the
# recursion, not a target.
MIN_CHUNK_LINES = 4


def tokenize_lyric_line(text: str) -> list[str]:
    """Mirror of `tokenizeLyricLine` in tauka/lib/src/utils/lyric_tokens.dart.

    Deliberately naive — split on space, drop empties, strip nothing — because
    the renderer is the specification and word timings are stored positionally
    against these indices. A tokenizer that disagrees here shifts every index
    after the disagreement, and nothing throws: the wrong word simply lights up.
    """
    return [w for w in text.split(" ") if w]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def split_for_retry(lines: list[str]) -> list[list[str]]:
    """Halve a line list that overflowed. Returns one or two chunks.

    Halving rather than re-chunking to a fixed size: the response was truncated,
    so the only thing known is that it did not fit. Halving converges in
    log(n) attempts and keeps chunks as large as the model will take, which is
    what keeps a verse's sense intact.
    """
    if len(lines) <= MIN_CHUNK_LINES:
        return [lines]
    mid = len(lines) // 2
    return [lines[:mid], lines[mid:]]


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_response(raw: str) -> list[dict]:
    """Pull the line array out of a model response.

    Tolerant of a fenced ```json block and of leading prose, because
    `response_format` is requested but not relied on — a provider that ignores
    it should cost a repair, not a failed song.

    Raises ValueError when there is no usable JSON, which the caller turns into
    one retry.
    """
    text = raw.strip()
    m = _FENCED.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith(("{", "[")):
        start = min(
            (i for i in (text.find("{"), text.find("[")) if i >= 0),
            default=-1,
        )
        if start < 0:
            raise ValueError("no JSON in response")
        text = text[start:]
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable JSON: {exc}") from exc

    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("lines", "lyrics", "result"):
            if isinstance(doc.get(key), list):
                return doc[key]
    raise ValueError("JSON did not contain a line array")


@dataclass
class ValidationResult:
    ok: bool
    lines: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.problems[:6]) or "ok"


MAX_FIELD_CHARS = 2_000


def validate(sent: list[str], returned: list[dict],
             toc_notes: set[str] | None = None) -> ValidationResult:
    """Structural checks from plan §6.1. Hard failures, not warnings.

    A wrong gloss is indistinguishable from a right one to a learner, and nearly
    indistinguishable to a tutor reading thirty lines at speed. These are the
    checks a machine can make so the human can spend attention on the ones it
    cannot.
    """
    problems: list[str] = []

    if len(returned) != len(sent):
        problems.append(
            f"expected {len(sent)} lines, got {len(returned)}")
        return ValidationResult(False, [], problems)

    for i, (original, entry) in enumerate(zip(sent, returned)):
        if not isinstance(entry, dict):
            problems.append(f"line {i}: not an object")
            continue

        echoed = entry.get("original_phrase")
        if echoed != original:
            # The anchor for everything else. Without a byte-identical echo
            # there is no way to tell a dropped line from a reordered one.
            problems.append(
                f"line {i}: echo differs ({echoed!r} != {original!r})")
            continue

        wb = entry.get("word_breakdown")
        if not isinstance(wb, list) or (original.strip() and not wb):
            problems.append(f"line {i}: empty word_breakdown")
            continue

        expected = len(tokenize_lyric_line(original))
        if len(wb) != expected:
            problems.append(
                f"line {i}: {len(wb)} word entries for {expected} tokens")

        for field_name in ("english_translation", "implied_meaning"):
            v = entry.get(field_name)
            if isinstance(v, str) and len(v) > MAX_FIELD_CHARS:
                # Runaway generation is also a runaway bill.
                problems.append(f"line {i}: {field_name} over length cap")

        for w in wb:
            if not isinstance(w, dict):
                problems.append(f"line {i}: malformed word entry")
                break
            ref = w.get("course_ref")
            if ref is None:
                continue
            if not isinstance(ref, dict) or not isinstance(ref.get("unit"), int):
                problems.append(f"line {i}: malformed course_ref {ref!r}")
                continue
            note = ref.get("note")
            if toc_notes is not None and note is not None and note not in toc_notes:
                # A reference outside the table of contents is a fabrication,
                # not a gap in the course. Drop it rather than ship a pill that
                # goes nowhere.
                problems.append(
                    f"line {i}: course_ref note {note!r} is not in the TOC")
                w.pop("course_ref", None)

    return ValidationResult(not problems, returned, problems)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

_LINE_FIELDS = ("english_translation", "implied_meaning")
_WORD_FIELDS = ("literal_meaning", "part_of_speech")


def merge(existing: list[dict], annotated: list[dict],
          overwrite: bool = False) -> tuple[list[dict], int]:
    """Fold annotations into the existing lines. Returns ``(lines, filled)``.

    **Fills empty only unless asked otherwise.** A tutor who hand-corrected a
    line must not lose it to the next run, and for the retrofit this is not a
    preference but the whole mechanism: association adds a field and rewrites
    nothing.

    Timings are copied through untouched. `start_ms`, `end_ms` and
    `word_start_ms` are none of the annotation pass's business, and a test
    should assert it never writes them.
    """
    out: list[dict] = []
    filled = 0

    for i, line in enumerate(existing):
        merged = dict(line)
        if i < len(annotated) and isinstance(annotated[i], dict):
            fresh = annotated[i]
            if fresh.get("original_phrase") == line.get("original_phrase"):
                for f in _LINE_FIELDS:
                    if fresh.get(f) and (overwrite or not merged.get(f)):
                        merged[f] = fresh[f]
                        filled += 1

                old_wb = merged.get("word_breakdown") or []
                new_wb = fresh.get("word_breakdown") or []
                if not old_wb:
                    merged["word_breakdown"] = new_wb
                    filled += len(new_wb)
                elif len(old_wb) == len(new_wb):
                    combined = []
                    for ow, nw in zip(old_wb, new_wb):
                        w = dict(ow)
                        for f in _WORD_FIELDS:
                            if nw.get(f) and (overwrite or not w.get(f)):
                                w[f] = nw[f]
                        if nw.get("course_ref") and (
                                overwrite or not w.get("course_ref")):
                            w["course_ref"] = nw["course_ref"]
                        if nw.get("confidence"):
                            w["confidence"] = nw["confidence"]
                        combined.append(w)
                    merged["word_breakdown"] = combined
        out.append(merged)

    return out, filled
