"""Public-holiday generation (FR-A-06).

A built-in generator covers Slovakia and the Czech Republic — the launch
jurisdictions — plus a generic Western-European set. Dates are computed, not
hard-coded per year, so any year can be imported. Anything else can be entered
by hand or bulk-loaded through the API.
"""

from __future__ import annotations

from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _common_movable(year: int) -> list[tuple[date, str]]:
    easter = easter_sunday(year)
    return [
        (easter - timedelta(days=2), "Good Friday"),
        (easter + timedelta(days=1), "Easter Monday"),
    ]


SK = [
    ((1, 1), "Day of the Establishment of the Slovak Republic"),
    ((1, 6), "Epiphany"),
    ((5, 1), "Labour Day"),
    ((5, 8), "Day of Victory over Fascism"),
    ((7, 5), "St Cyril and St Methodius Day"),
    ((8, 29), "Slovak National Uprising Anniversary"),
    ((9, 1), "Constitution Day"),
    ((9, 15), "Day of Our Lady of Sorrows"),
    ((11, 1), "All Saints' Day"),
    ((11, 17), "Struggle for Freedom and Democracy Day"),
    ((12, 24), "Christmas Eve"),
    ((12, 25), "Christmas Day"),
    ((12, 26), "St Stephen's Day"),
]

CZ = [
    ((1, 1), "New Year's Day / Restoration Day"),
    ((5, 1), "Labour Day"),
    ((5, 8), "Liberation Day"),
    ((7, 5), "St Cyril and St Methodius Day"),
    ((7, 6), "Jan Hus Day"),
    ((9, 28), "St Wenceslas Day"),
    ((10, 28), "Independent Czechoslovak State Day"),
    ((11, 17), "Struggle for Freedom and Democracy Day"),
    ((12, 24), "Christmas Eve"),
    ((12, 25), "Christmas Day"),
    ((12, 26), "St Stephen's Day"),
]

GENERIC = [
    ((1, 1), "New Year's Day"),
    ((5, 1), "Labour Day"),
    ((12, 25), "Christmas Day"),
    ((12, 26), "Boxing Day"),
]

CATALOGUE = {"SK": SK, "CZ": CZ}


def for_year(country: str, year: int) -> list[tuple[date, str]]:
    fixed = CATALOGUE.get(country.upper(), GENERIC)
    days = [(date(year, month, day), name) for (month, day), name in fixed]
    days.extend(_common_movable(year))
    return sorted(set(days))
