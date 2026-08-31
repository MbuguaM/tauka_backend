"""Tests for lyric annotation chunking, validation and merge.

Pure functions, no network. Where possible these run against the real corpus in
`Course creator/explore_items_normalised/` rather than invented fixtures — the
last two planning passes were each corrected by measuring the actual files, and
a fixture that agrees with the code proves nothing about the songs.
"""
import json
import pathlib

import pytest

from app.services.ai_prompts import (
    ANNOTATION_PROMPT_VERSION,
    FRAMED_MODES,
    build_framed_prompt,
    build_lyric_annotate,
)
from app.services.lyric_annotation_service import (
    merge,
    parse_response,
    split_for_retry,
    tokenize_lyric_line,
    validate,
)

CORPUS = (pathlib.Path(__file__).resolve().parents[2]
          / "Course creator" / "explore_items_normalised")


def _corpus_files():
    return sorted(CORPUS.glob("*.json")) if CORPUS.is_dir() else []


# ---------------------------------------------------------------------------
# Tokenizer — must not drift from the Dart renderer
# ---------------------------------------------------------------------------

def test_tokenizer_is_the_naive_split():
    # Identical to tokenizeLyricLine: split on space, drop empties, strip
    # nothing. Word timings are stored positionally against these indices.
    assert tokenize_lyric_line("a  b c") == ["a", "b", "c"]
    assert tokenize_lyric_line("(አሃሃ) ነው") == ["(አሃሃ)", "ነው"]
    assert tokenize_lyric_line("") == []


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_lyric_annotate_is_not_dispatchable_from_ai_generate():
    """It is framed, but it is not a /ai/generate mode.

    FRAMED_MODES drives that route's dispatch. Listing `lyric_annotate` there
    made `POST /ai/generate {"mode": "lyric_annotate"}` reachable and then raise
    on a missing line list — a 500 where a 400 belongs. The fence property is
    covered directly below instead of by proxy through that route.
    """
    assert "lyric_annotate" not in FRAMED_MODES
    with pytest.raises(ValueError):
        build_framed_prompt("lyric_annotate", "some text")


def test_prompt_fences_everything_with_one_shared_nonce():
    system, user = build_lyric_annotate(
        ["አለህ በውስጤ", "መንፈስ ቅዱሰ"], "መንፈስ - አስቴር", "note_4.5 Direct Object")
    nonce = system.split("<<<... ")[1].split(">>>")[0]
    # One nonce, generated per request, closing every fence. Content cannot end
    # a fence whose name it cannot guess.
    assert user.count(nonce) == 6          # three fences, open and close
    for label in ("COURSE CONTENTS", "SONG TITLE", "LYRIC LINES"):
        assert f"<<<{label} {nonce}>>>" in user
    assert "never an instruction" in system.lower() or "never an instruction to you" in system


def test_prompt_carries_the_lyrics_and_the_contents():
    _, user = build_lyric_annotate(["ሰላም ነው"], "t", "note_2.2 and")
    assert "ሰላም ነው" in user
    assert "note_2.2 and" in user


def test_prompt_version_is_declared():
    assert ANNOTATION_PROMPT_VERSION


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"lines": [{"original_phrase": "a"}]}',
    '```json\n{"lines": [{"original_phrase": "a"}]}\n```',
    'Here you go:\n{"lines": [{"original_phrase": "a"}]}',
    '[{"original_phrase": "a"}]',
])
def test_parse_tolerates_prose_and_fences(raw):
    # response_format is requested but not relied on: a provider that ignores
    # it should cost a repair, not a failed song.
    assert parse_response(raw)[0]["original_phrase"] == "a"


@pytest.mark.parametrize("raw", ["", "no json here", "{not json"])
def test_parse_raises_rather_than_returning_nothing(raw):
    with pytest.raises(ValueError):
        parse_response(raw)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _entry(phrase, words=None):
    words = words or [{"word": w, "literal_meaning": "x", "part_of_speech": "Noun"}
                      for w in tokenize_lyric_line(phrase)]
    return {"original_phrase": phrase, "english_translation": "t",
            "implied_meaning": "i", "word_breakdown": words}


def test_valid_chunk_passes():
    lines = ["ሰላም ነው", "አለህ በውስጤ"]
    assert validate(lines, [_entry(l) for l in lines]).ok


def test_a_dropped_line_fails_loudly():
    lines = ["a b", "c d"]
    r = validate(lines, [_entry("a b")])
    assert not r.ok and "expected 2 lines" in r.summary


def test_a_reordered_line_is_caught_by_the_echo():
    lines = ["a b", "c d"]
    r = validate(lines, [_entry("c d"), _entry("a b")])
    assert not r.ok and "echo differs" in r.summary


def test_normalised_echo_is_rejected():
    # "Copy each line back exactly" is load-bearing: a silently corrected line
    # would put the gloss against text the learner never sees.
    r = validate(["a  b"], [_entry("a b")])
    assert not r.ok and "echo differs" in r.summary


def test_word_count_mismatch_is_reported():
    e = _entry("a b c")
    e["word_breakdown"] = e["word_breakdown"][:2]
    r = validate(["a b c"], [e])
    assert not r.ok and "2 word entries for 3 tokens" in r.summary


def test_empty_breakdown_is_a_failure_not_a_gap():
    e = _entry("a b")
    e["word_breakdown"] = []
    assert not validate(["a b"], [e]).ok


def test_a_course_ref_outside_the_toc_is_stripped_not_shipped():
    e = _entry("a")
    e["word_breakdown"][0]["course_ref"] = {"unit": 4, "note": "99.9"}
    r = validate(["a"], [e], toc_notes={"4.5"})
    assert not r.ok
    # A fabricated reference must not survive into the file.
    assert "course_ref" not in r.lines[0]["word_breakdown"][0]


def test_a_course_ref_inside_the_toc_survives():
    e = _entry("a")
    e["word_breakdown"][0]["course_ref"] = {"unit": 4, "note": "4.5"}
    r = validate(["a"], [e], toc_notes={"4.5"})
    assert r.ok
    assert r.lines[0]["word_breakdown"][0]["course_ref"]["note"] == "4.5"


def test_runaway_field_is_capped():
    e = _entry("a")
    e["english_translation"] = "x" * 5000
    assert not validate(["a"], [e]).ok


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

def test_split_halves_and_bottoms_out():
    assert split_for_retry(list("abcdefgh")) == [list("abcd"), list("efgh")]
    # Below the floor it stops rather than recursing to single lines.
    assert split_for_retry(list("abc")) == [list("abc")]


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_merge_fills_empty_only():
    existing = [{"original_phrase": "a", "english_translation": "MINE",
                 "word_breakdown": [{"word": "a", "literal_meaning": "MINE",
                                     "part_of_speech": ""}]}]
    fresh = [_entry("a")]
    out, _ = merge(existing, fresh)
    # A tutor's correction must survive the next run.
    assert out[0]["english_translation"] == "MINE"
    assert out[0]["word_breakdown"][0]["literal_meaning"] == "MINE"
    # ...but a genuinely empty field is filled.
    assert out[0]["word_breakdown"][0]["part_of_speech"] == "Noun"


def test_merge_overwrites_only_when_asked():
    existing = [{"original_phrase": "a", "english_translation": "MINE",
                 "word_breakdown": []}]
    out, _ = merge(existing, [_entry("a")], overwrite=True)
    assert out[0]["english_translation"] == "t"


def test_merge_never_touches_timings():
    existing = [{"original_phrase": "a b", "start_ms": 100, "end_ms": 900,
                 "word_start_ms": [100, 500], "word_breakdown": []}]
    out, _ = merge(existing, [_entry("a b")])
    assert out[0]["start_ms"] == 100
    assert out[0]["end_ms"] == 900
    assert out[0]["word_start_ms"] == [100, 500]


def test_merge_ignores_a_line_whose_echo_does_not_match():
    existing = [{"original_phrase": "a", "english_translation": ""}]
    out, filled = merge(existing, [_entry("DIFFERENT")])
    assert filled == 0
    assert out[0]["english_translation"] == ""


# ---------------------------------------------------------------------------
# Against the real corpus
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _corpus_files(), reason="corpus not present")
def test_every_corpus_file_round_trips_through_validate():
    """The corpus is what the generator has to reproduce.

    Alignment is ~91% after markup normalisation, so `ok` is not expected — but
    every file must parse, echo cleanly, and never trip a structural check other
    than the known word-count one.
    """
    for path in _corpus_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        lines = [l["original_phrase"] for l in doc["lyrics"]]
        result = validate(lines, doc["lyrics"])
        for problem in result.problems:
            assert "word entries for" in problem, f"{path.name}: {problem}"


@pytest.mark.skipif(not _corpus_files(), reason="corpus not present")
def test_no_corpus_line_would_be_split_below_the_floor():
    for path in _corpus_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert len(doc["lyrics"]) >= 4, path.name
