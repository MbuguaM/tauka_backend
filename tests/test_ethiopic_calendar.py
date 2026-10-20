"""Ethiopic calendar (enrolment plan 1.2) against the SHARED vectors.

The same JSON drives the JS and Dart tests, so a disagreement between the three
implementations shows up as a failure in whichever one is wrong.
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core.ethiopic_calendar import (
    EthiopicDate, from_gregorian, next_intake, season_of, to_geez, to_gregorian,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "shared-features" / "contracts" / "ethiopic_date_vectors.json")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize("v", VECTORS["conversions"], ids=lambda v: v["gregorian"])
def test_conversion_both_ways(v):
    g = date.fromisoformat(v["gregorian"])
    ec = EthiopicDate(*v["ec"])
    assert from_gregorian(g) == ec
    assert to_gregorian(ec) == g


@pytest.mark.parametrize("v", VECTORS["invalid_ec"], ids=lambda v: v["note"])
def test_invalid_ec_dates_are_refused(v):
    with pytest.raises(ValueError):
        to_gregorian(EthiopicDate(*v["ec"]))


@pytest.mark.parametrize("n,expected", VECTORS["geez"])
def test_geez(n, expected):
    assert to_geez(n) == expected


@pytest.mark.parametrize("n", VECTORS["geez_invalid"])
def test_geez_out_of_range(n):
    with pytest.raises(ValueError):
        to_geez(n)


@pytest.mark.parametrize("v", VECTORS["seasons"], ids=lambda v: v["gregorian"])
def test_season(v):
    assert season_of(date.fromisoformat(v["gregorian"])) == (v["season"], v["ec_year"])


@pytest.mark.parametrize("v", VECTORS["next_intake"], ids=lambda v: v["on"])
def test_next_intake(v):
    got = next_intake(VECTORS["intakes"], date.fromisoformat(v["on"]))
    assert (got["ethiopic_month"] if got else None) == v["expect"]


def test_round_trip_every_day_for_two_centuries():
    d, end = date(1900, 1, 1), date(2100, 12, 31)
    while d <= end:
        assert to_gregorian(from_gregorian(d)) == d
        d += timedelta(days=1)
