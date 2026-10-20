"""Ethiopic calendar: Gregorian <-> E.C., Ge'ez numerals, seasons, next intake.

Enrolment plan Track 1.2. There is ONE implementation per language (this file,
tauka-react-web `src/utilities/ethiopic.js`, tauka-flutter
`lib/src/utils/ethiopic_calendar.dart`) and ONE shared vector file they are all
tested against: `shared-features/contracts/ethiopic_date_vectors.json`. Change
the behaviour in all three together, or add a vector first and watch two fail.

Dates are calendar dates, never instants: everything goes through the Julian
Day Number, so no timezone or DST can move a day. (The JS port matters most
here — a local-time `Date` drifts across a DST change.)

The calendar: 12 months of 30 days, then Pagume with 5 days, or 6 in a leap
year. An E.C. year is leap when `year % 4 == 3` (2019 E.C. is one: Pagume 6 =
11 Sep 2027). The year starts on Meskerem 1, 11 Sep — 12 Sep after a leap year.
"""

from dataclasses import dataclass
from datetime import date

# JDN of Meskerem 1, 1 E.C. (Amete Mihret epoch).
_EPOCH = 1724221
# date.toordinal() is days since 0001-01-01 (ordinal 1); JDN of that day.
_ORDINAL_TO_JDN = 1721425

MONTHS = (
    "meskerem", "tikimt", "hidar", "tahsas", "tir", "yekatit", "megabit",
    "miyazya", "ginbot", "sene", "hamle", "nehase", "pagume",
)

# Plan §3 / the playbook's season table. Pagume belongs to Kiremt.
_SEASON_OF_MONTH = (
    "tsedey", "tsedey", "tsedey",
    "bega", "bega", "bega",
    "belg", "belg", "belg",
    "kiremt", "kiremt", "kiremt", "kiremt",
)


@dataclass(frozen=True)
class EthiopicDate:
    year: int
    month: int  # 1..13
    day: int    # 1..30 (Pagume 1..5 or 6)

    @property
    def month_name(self) -> str:
        return MONTHS[self.month - 1]


def is_leap(ec_year: int) -> bool:
    return ec_year % 4 == 3


def _validate(year: int, month: int, day: int) -> None:
    if not 1 <= month <= 13:
        raise ValueError(f"month {month} is not 1..13")
    last = (6 if is_leap(year) else 5) if month == 13 else 30
    if not 1 <= day <= last:
        raise ValueError(f"day {day} is not 1..{last} in {MONTHS[month - 1]} {year}")


def to_jdn(ec: EthiopicDate) -> int:
    _validate(ec.year, ec.month, ec.day)
    return _EPOCH + 365 * (ec.year - 1) + ec.year // 4 + 30 * (ec.month - 1) + ec.day - 1


def from_jdn(jdn: int) -> EthiopicDate:
    # Each 4-year cycle holds 365, 365, 366, 365 days: the THIRD year is leap
    # (year % 4 == 3), matching to_jdn's `year // 4`.
    days = jdn - _EPOCH
    cycle, rem = divmod(days, 1461)
    if rem < 1096:
        year_in_cycle = min(rem // 365, 2)
        day_of_year = rem - 365 * year_in_cycle
    else:
        year_in_cycle = 3
        day_of_year = rem - 1096
    year = 4 * cycle + year_in_cycle + 1
    month, day0 = divmod(day_of_year, 30)
    return EthiopicDate(year, month + 1, day0 + 1)


def from_gregorian(d: date) -> EthiopicDate:
    return from_jdn(d.toordinal() + _ORDINAL_TO_JDN)


def to_gregorian(ec: EthiopicDate) -> date:
    return date.fromordinal(to_jdn(ec) - _ORDINAL_TO_JDN)


def season_of(d: date) -> tuple[str, int]:
    """(season name, E.C. year) of a Gregorian date, e.g. ('bega', 2019)."""
    ec = from_gregorian(d)
    return _SEASON_OF_MONTH[ec.month - 1], ec.year


# ── Ge'ez numerals ────────────────────────────────────────────────────────────

_ONES = ("", "፩", "፪", "፫", "፬", "፭", "፮", "፯", "፰", "፱")
_TENS = ("", "፲", "፳", "፴", "፵", "፶", "፷", "፸", "፹", "፺")
_HUNDRED = "፻"


def _pair(n: int) -> str:
    return _TENS[n // 10] + _ONES[n % 10]


def to_geez(n: int) -> str:
    """Ge'ez numeral for 1..9999 (years and days need no more).

    There is no zero. 100 is ፻, not ፩፻; 2019 is ፳፻፲፱.
    """
    if not 1 <= n <= 9999:
        raise ValueError(f"{n} is outside 1..9999")
    hi, lo = divmod(n, 100)
    if hi == 0:
        return _pair(lo)
    return ("" if hi == 1 else _pair(hi)) + _HUNDRED + _pair(lo)


# ── Intakes ───────────────────────────────────────────────────────────────────

def next_intake(intakes: list[dict], on: date) -> dict | None:
    """The first SCHEDULED intake forming on or after `on`, from app.intake rows.

    Reads the table's `formation_date`, never a computed month day 1: an admin
    can move a formation (Megabit 2019 → 11 Mar), and the table is the truth.
    Rows need `formation_date` (date or ISO string) and `status`.
    """
    def fd(row):
        v = row["formation_date"]
        return v if isinstance(v, date) else date.fromisoformat(v)

    upcoming = [r for r in intakes if r.get("status", "scheduled") == "scheduled" and fd(r) >= on]
    return min(upcoming, key=fd) if upcoming else None
