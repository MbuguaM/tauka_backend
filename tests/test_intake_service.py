"""The DB-facing half of the cohort matcher — config parsing and the enums.

`cohort_matcher` is pure and is tested exhaustively in `test_cohort_matcher.py`.
What is left here are the two places where a mistake is silent rather than loud:
a config row that fails to parse, and a deferral reason with no sentence
attached. Both degrade into something that *looks* fine.
"""

import pytest

from app.services import intake_service, offer_service
from app.services.cohort_matcher import MatchConfig


def _rows(**kv):
    return [{"key": f"match.{k}", "value": v} for k, v in kv.items()]


# ── Config ───────────────────────────────────────────────────────────────────

def test_an_empty_config_is_the_numbers_the_plan_chose():
    """No rows at all must not mean no bands. Every default here is a number
    argued for in `tutor-cohort-matching-plan.md` §3."""
    cfg = intake_service._rows_to_config([])
    assert (cfg.tz_band_hours, cfg.lesson_band) == (2.0, 5)
    assert (cfg.cohort_min, cfg.cohort_max) == (3, 4)
    assert cfg.offer_ttl_hours == 48
    assert cfg.weights["goal"] == 30.0


def test_config_values_override_the_defaults():
    cfg = intake_service._rows_to_config(
        _rows(tz_band_hours="3.5", lesson_band="8", offer_ttl_hours="24"))
    assert cfg.tz_band_hours == 3.5
    assert cfg.lesson_band == 8
    assert cfg.offer_ttl_hours == 24


@pytest.mark.parametrize("bad", ["", "  ", "two", "3;DROP", None])
def test_an_unparseable_value_degrades_to_the_default_and_never_raises(bad):
    """A bad config row must not be able to stop a cycle — only to make it run
    on the documented numbers. This is the same fail-soft rule
    `matching_service._config` and `app.generate_cohort_sessions` already keep.
    """
    cfg = intake_service._rows_to_config(_rows(lesson_band=bad))
    assert cfg.lesson_band == 5


def test_civil_hours_parse_into_minutes_and_survive_a_malformed_range():
    cfg = intake_service._rows_to_config(_rows(civil_hours="08:30-21:00"))
    assert (cfg.civil_start_minute, cfg.civil_end_minute) == (8 * 60 + 30, 21 * 60)

    broken = intake_service._rows_to_config(_rows(civil_hours="morning-ish"))
    assert (broken.civil_start_minute, broken.civil_end_minute) == (7 * 60, 22 * 60)


def test_partial_weights_merge_rather_than_replace():
    """A config row naming three weights must not silently zero the other seven.

    This is the failure that would be hardest to see: the cycle runs, every
    score is wrong, and nothing errors. Merging means a partial override is a
    partial override.
    """
    cfg = intake_service._rows_to_config(_rows(weights='{"goal": 40, "style": 5}'))
    assert cfg.weights["goal"] == 40.0
    assert cfg.weights["style"] == 5.0
    assert cfg.weights["tier_cohesion"] == 20.0   # untouched
    assert set(cfg.weights) == set(MatchConfig().weights)


def test_an_unknown_weight_key_is_ignored_rather_than_added():
    """A typo must not create a weight nothing reads — it must leave the real
    one at its default, where it is at least visible in the snapshot."""
    cfg = intake_service._rows_to_config(_rows(weights='{"goals": 99}'))
    assert "goals" not in cfg.weights
    assert cfg.weights["goal"] == 30.0


def test_the_config_snapshot_round_trips_every_field():
    """`intake_cycle.config` is what makes a past cycle explicable. A field
    missing from `as_dict` is a field no past result can be defended on."""
    cfg = MatchConfig()
    snapshot = cfg.as_dict()
    for field in ("tz_band_hours", "lesson_band", "cohort_min", "cohort_max",
                  "offer_ttl_hours", "overbook_slack", "relax_after_cycles",
                  "referral_preference", "weights"):
        assert field in snapshot
    assert snapshot["weights"] == cfg.weights
    # A copy, not the live dict — a snapshot that aliases the config would
    # change under a later edit.
    snapshot["weights"]["goal"] = 0
    assert cfg.weights["goal"] == 30.0


# ── Time parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, minutes", [
    ("09:00:00", 540), ("09:30:00", 570), ("00:00:00", 0),
    ("23:59:00", 1439), ("18:00", 1080),
])
def test_times_parse_to_minutes_past_midnight(raw, minutes):
    assert intake_service._minutes(raw) == minutes


@pytest.mark.parametrize("raw", [None, "", "nine", "::"])
def test_an_unparseable_time_is_midnight_rather_than_an_exception(raw):
    assert intake_service._minutes(raw) == 0


# ── Native language, which is an inference ───────────────────────────────────

def test_native_language_takes_the_strongest_declared_language():
    known = [{"name": "English", "proficiency": 3},
             {"name": "Amharic", "proficiency": 5}]
    assert intake_service._native_language(known) == "Amharic"


@pytest.mark.parametrize("known", [None, [], "Amharic", [{}], [{"proficiency": 5}]])
def test_native_language_is_none_rather_than_a_guess(known):
    """There is no `native_language` column on the student side, so this is a
    proxy — and it feeds only a soft, decaying bonus. Returning None where the
    data does not support an answer keeps the blast radius right."""
    assert intake_service._native_language(known) is None


# ── The deferral enum, on both sides ─────────────────────────────────────────

# The CHECK constraint on `app.deferred_student.reason`, migration §86.3.
DB_REASONS = {
    "NO_TUTOR_FOR_LEVEL", "NO_SLOT_OVERLAP", "TZ_ISOLATE",
    "TOO_FEW_PEERS_AT_LEVEL", "TUTORS_AT_CAPACITY", "DECLINED_ALL_OFFERS",
    "NO_TIMEZONE", "TOO_FEW_PREFERENCES", "NO_LEVEL",
    "COHORT_DID_NOT_FILL", "OFFERS_EXPIRED",
}


def test_every_deferral_reason_has_a_sentence():
    """A reason with no message renders the generic fallback — which is exactly
    the "no match found" that plan §4b Stage 1 rejects by name. The failure is
    silent: the student gets a sentence, just not the true one.
    """
    missing = DB_REASONS - set(offer_service.DEFERRAL_MESSAGES)
    assert not missing, f"deferral reasons with no student-facing message: {missing}"


def test_no_message_exists_for_a_reason_the_database_would_refuse():
    """The other direction. A message keyed on a reason the CHECK rejects is
    dead code that reads as coverage."""
    extra = set(offer_service.DEFERRAL_MESSAGES) - DB_REASONS
    assert not extra, f"messages for reasons the CHECK constraint refuses: {extra}"


def test_the_three_self_serviceable_reasons_are_the_ones_a_form_fixes():
    """The client shows a button for these and an explanation for the rest.
    Getting this set wrong means either asking a student to fix a tutor shortage
    or hiding the form that would have placed them."""
    self_serviceable = {"NO_TIMEZONE", "TOO_FEW_PREFERENCES", "NO_LEVEL"}
    for reason in self_serviceable:
        assert reason in DB_REASONS
    # Each of them names the thing to do, not the thing that failed.
    for reason in self_serviceable:
        message = offer_service.DEFERRAL_MESSAGES[reason]
        assert message and message[-1] == "."


def test_silence_and_refusal_are_different_reasons():
    """`DECLINED_ALL_OFFERS` is plan §3 Stage 6's *scoring signal* — "the offers
    were feasible and unattractive". Pouring non-response into it destroys the
    one signal that says so, which is why `OFFERS_EXPIRED` exists."""
    assert offer_service.DEFERRAL_MESSAGES["DECLINED_ALL_OFFERS"] != \
        offer_service.DEFERRAL_MESSAGES["OFFERS_EXPIRED"]
    assert "turned down" in offer_service.DEFERRAL_MESSAGES["DECLINED_ALL_OFFERS"]


# ── The intake-day guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, day, expected", [
    ("1,15", 1, True), ("1,15", 15, True), ("1,15", 7, False),
    ("1, 15 ", 15, True), ("", 1, True), ("nonsense", 15, True),
])
def test_intake_day_parsing(raw, day, expected, monkeypatch):
    """The cadence guard, same shape as `payout.cadence_day`. A malformed value
    falls back to the 1st and 15th rather than running every day or never."""
    class _Result:
        data = [{"value": raw}] if raw else [{}]

    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _Result()

    class _Schema:
        def table(self, *a, **k): return _Q()

    monkeypatch.setattr(intake_service.db, "schema", lambda *a, **k: _Schema())

    from datetime import date
    assert intake_service.is_intake_day(date(2026, 9, day)) is expected
