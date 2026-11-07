"""The Ethiopian clock (exchange-extension-plan XE-1; exchange docs 01 §7.2, 06 §12).

Ethiopia keeps East Africa Time (UTC+3, no daylight saving) but counts the hours of the
day from 06:00: 07:00 is "1 in the morning", 19:00 is "1 at night". Reminders and times
for anyone in Ethiopia show both clocks:

    19:00 EAT · Ethiopian time 1:00 night (ማታ)

One implementation per client, with the same test vectors: this file,
tauka-react-web/src/utilities/ethiopianClock.js and tauka/lib/src/utils/ethiopian_clock.dart.
The period words need a native reviewer's sign-off before they ship in Amharic.
"""

from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))
PERIODS = {
    "morning": "ጠዋት",      # 06:00-11:59
    "afternoon": "ከሰዓት",   # 12:00-17:59
    "night": "ማታ",          # 18:00-23:59
    "late night": "ሌሊት",    # 00:00-05:59
}


def ethiopian_clock(at: datetime) -> tuple[int, int, str]:
    """(hour 1-12, minute, period) on the Ethiopian clock for an instant."""
    eat = at.astimezone(EAT)
    h = (eat.hour - 6) % 12
    if h == 0:
        h = 12
    if 6 <= eat.hour < 12:
        period = "morning"
    elif 12 <= eat.hour < 18:
        period = "afternoon"
    elif eat.hour >= 18:
        period = "night"
    else:
        period = "late night"
    return h, eat.minute, period


def both_clocks(at: datetime) -> str:
    """'19:00 EAT · Ethiopian time 1:00 night (ማታ)'."""
    eat = at.astimezone(EAT)
    h, m, period = ethiopian_clock(at)
    return f"{eat:%H:%M} EAT · Ethiopian time {h}:{m:02d} {period} ({PERIODS[period]})"


def in_ethiopia(country_code: str | None, tz: str | None) -> bool:
    return (country_code or "").upper() == "ET" or tz == "Africa/Addis_Ababa"
