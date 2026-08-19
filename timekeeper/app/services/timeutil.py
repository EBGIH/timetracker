"""Time arithmetic shared by the calculation and compliance engines.

All stored timestamps are naive UTC (section 12.3). Local time is derived from
the location's time zone, falling back to the organisation's.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]


# ---------------------------------------------------------------------------
# UTC / local conversion
# ---------------------------------------------------------------------------


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - misconfiguration fallback
        return ZoneInfo("UTC")


def to_local(dt_utc: datetime, tzname: str) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(tz(tzname))


def to_utc(dt_local: datetime, tzname: str) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz(tzname))
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_bounds(day: date, tzname: str) -> Interval:
    """UTC half-open interval [start, end) covering one local calendar day."""
    start = to_utc(datetime.combine(day, time(0, 0)), tzname)
    end = to_utc(datetime.combine(day + timedelta(days=1), time(0, 0)), tzname)
    return start, end


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc(dt: datetime | None) -> datetime | None:
    """Normalise an inbound timestamp to naive UTC.

    Clients send ISO 8601 with an offset; a bare `.replace(tzinfo=None)` would
    silently keep the local wall-clock reading, so the offset is applied first.
    A value with no offset is taken to be UTC already.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Interval algebra
# ---------------------------------------------------------------------------


def clip(interval: Interval, window: Interval) -> Interval | None:
    start = max(interval[0], window[0])
    end = min(interval[1], window[1])
    return (start, end) if end > start else None


def normalise(intervals: list[Interval]) -> list[Interval]:
    """Sort and merge overlapping intervals."""
    cleaned = sorted((s, e) for s, e in intervals if e > s)
    merged: list[Interval] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(base: list[Interval], cuts: list[Interval]) -> list[Interval]:
    result = normalise(base)
    for cut_start, cut_end in normalise(cuts):
        next_result: list[Interval] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                next_result.append((start, end))
                continue
            if start < cut_start:
                next_result.append((start, cut_start))
            if cut_end < end:
                next_result.append((cut_end, end))
        result = next_result
    return result


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for interval in normalise(a):
        for window in normalise(b):
            piece = clip(interval, window)
            if piece:
                out.append(piece)
    return normalise(out)


def total_minutes(intervals: list[Interval]) -> int:
    seconds = sum((e - s).total_seconds() for s, e in normalise(intervals))
    return int(round(seconds / 60))


def take_last_minutes(intervals: list[Interval], minutes: int) -> list[Interval]:
    """The trailing `minutes` of a set of intervals, chronologically."""
    if minutes <= 0:
        return []
    remaining = minutes
    out: list[Interval] = []
    for start, end in reversed(normalise(intervals)):
        span = int(round((end - start).total_seconds() / 60))
        if span <= remaining:
            out.append((start, end))
            remaining -= span
        else:
            out.append((end - timedelta(minutes=remaining), end))
            remaining = 0
        if remaining == 0:
            break
    return normalise(out)


def night_windows(
    window: Interval, tzname: str, night_start: str, night_end: str
) -> list[Interval]:
    """Night periods (WT-06, default 22:00-06:00) inside a UTC window,
    expressed in UTC. Handles the wrap across midnight."""
    start_t = parse_hhmm(night_start)
    end_t = parse_hhmm(night_end)
    first_day = to_local(window[0], tzname).date() - timedelta(days=1)
    last_day = to_local(window[1], tzname).date() + timedelta(days=1)
    out: list[Interval] = []
    day = first_day
    while day <= last_day:
        if start_t <= end_t:
            seg = (
                to_utc(datetime.combine(day, start_t), tzname),
                to_utc(datetime.combine(day, end_t), tzname),
            )
            piece = clip(seg, window)
            if piece:
                out.append(piece)
        else:
            seg = (
                to_utc(datetime.combine(day, start_t), tzname),
                to_utc(datetime.combine(day + timedelta(days=1), end_t), tzname),
            )
            piece = clip(seg, window)
            if piece:
                out.append(piece)
        day += timedelta(days=1)
    return normalise(out)


# ---------------------------------------------------------------------------
# Rounding (FR-A-09 / BR-07)
# ---------------------------------------------------------------------------


def round_timestamp(dt: datetime, minutes: int, direction: str) -> datetime:
    """Rounding is applied to clock-in and clock-out only, never to computed
    totals, and in the same direction for both events (BR-07). 'nearest' is the
    symmetric default; 'up' and 'down' are available where a collective
    agreement requires them."""
    if not minutes or minutes <= 1:
        return dt
    anchor = dt.replace(second=0, microsecond=0)
    offset = anchor.minute % minutes
    seconds_part = dt.second + dt.microsecond / 1_000_000
    if direction == "up":
        if offset or seconds_part:
            anchor += timedelta(minutes=minutes - offset)
        return anchor
    if direction == "down":
        return anchor - timedelta(minutes=offset)
    # nearest
    total = offset + seconds_part / 60
    if total * 2 >= minutes:
        anchor += timedelta(minutes=minutes - offset)
    else:
        anchor -= timedelta(minutes=offset)
    return anchor


# ---------------------------------------------------------------------------
# Display helpers (FR-A-04)
# ---------------------------------------------------------------------------


def format_duration(minutes: int, fmt: str = "hm") -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    if fmt == "decimal":
        return f"{sign}{minutes / 60:.2f}"
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Attendance periods (FR-A-05)
# ---------------------------------------------------------------------------


def period_bounds(period_type: str, day: date, week_start: int = 0,
                  anchor: date | None = None) -> Interval:
    """Returns the (start_date, end_date) inclusive bounds of the attendance
    period containing `day`."""
    if period_type == "weekly":
        delta = (day.weekday() - week_start) % 7
        start = day - timedelta(days=delta)
        return start, start + timedelta(days=6)
    if period_type == "biweekly":
        base = anchor or date(day.year, 1, 1)
        delta = (base.weekday() - week_start) % 7
        base = base - timedelta(days=delta)
        weeks = ((day - base).days // 7) // 2 * 2
        start = base + timedelta(weeks=weeks)
        return start, start + timedelta(days=13)
    if period_type == "semimonthly":
        if day.day <= 15:
            return date(day.year, day.month, 1), date(day.year, day.month, 15)
        return date(day.year, day.month, 16), _month_end(day)
    # monthly (default)
    return date(day.year, day.month, 1), _month_end(day)


def _month_end(day: date) -> date:
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def iso_week_bounds(day: date, week_start: int = 0) -> Interval:
    delta = (day.weekday() - week_start) % 7
    start = day - timedelta(days=delta)
    return start, start + timedelta(days=6)
