"""Working-time rules engine (specification section 16) and operational
exception detection.

Every threshold is a parameter, never a constant, because national law is
frequently stricter than the Directive. Parameters are versioned
(RuleParamVersion) so a historic evaluation reflects the parameters in force
at the time.

Rules are evaluated twice: on write, for same-day feedback, and in a nightly
batch for rolling-window rules such as WT-01.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AttendanceException,
    AttendanceSession,
    DayAggregate,
    Organisation,
    RuleParamVersion,
    User,
    new_id,
    utcnow,
)
from . import calc, timeutil as T

DEFAULT_PARAMS: dict[str, int] = {
    # WT-01 maximum average weekly working time
    "wt01_max_avg_weekly_minutes": 48 * 60,
    "wt01_reference_months": 4,
    # WT-02 minimum daily rest
    "wt02_min_daily_rest_minutes": 11 * 60,
    # WT-03 minimum weekly rest
    "wt03_min_weekly_rest_minutes": 24 * 60,
    # WT-04 rest break
    "wt04_break_after_minutes": 6 * 60,
    "wt04_min_break_minutes": 30,
    # WT-05 night work limit
    "wt05_night_avg_max_minutes": 8 * 60,
    "wt05_reference_months": 4,
    # WT-07 annual paid leave minimum
    "wt07_annual_leave_weeks": 4,
    # Operational
    "daily_max_minutes": 12 * 60,
}

# type -> (severity, blocks period submission)
EXCEPTION_META: dict[str, tuple[str, bool]] = {
    "OPEN_SESSION": ("error", True),
    "MISSING_CLOCK_OUT": ("error", True),
    "LONG_SESSION": ("warning", True),
    "UNCONFIRMED_AUTO_STOP": ("warning", True),
    "UNEXPLAINED_ABSENCE": ("warning", True),
    "BREAK_SHORTFALL": ("error", False),
    "DAILY_MAX_EXCEEDED": ("error", False),
    "MIN_REST": ("error", False),
    "WEEKLY_REST": ("error", False),
    "MAX_WEEKLY_AVERAGE": ("error", False),
    "NIGHT_WORK_LIMIT": ("warning", False),
    "UNAPPROVED_OVERTIME": ("warning", False),
    "OVERLAP": ("error", True),
}

BLOCKING_TYPES = {name for name, (_s, blocking) in EXCEPTION_META.items() if blocking}


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def rule_params(db: Session, org: Organisation, day: date | None = None) -> dict:
    """The parameters in force on `day`.

    Versions are applied cumulatively in chronological order, so a version that
    changes one threshold leaves the others as they were. A version that takes
    effect after `day` is deliberately not applied: a historic evaluation must
    reflect the parameters in force at the time (section 16).
    """
    params = dict(DEFAULT_PARAMS)
    if day is None:
        params.update(
            {k: v for k, v in (org.settings_json or {}).items() if k in DEFAULT_PARAMS}
        )
        return params
    versions = db.scalars(
        select(RuleParamVersion)
        .where(
            RuleParamVersion.org_id == org.id,
            RuleParamVersion.effective_from <= day,
        )
        .order_by(RuleParamVersion.effective_from, RuleParamVersion.created_at)
    ).all()
    for version in versions:
        params.update({k: v for k, v in (version.params or {}).items() if k in DEFAULT_PARAMS})
    return params


def save_rule_params(
    db: Session, org: Organisation, params: dict, effective_from: date, actor_id: str
) -> RuleParamVersion:
    clean = {k: v for k, v in params.items() if k in DEFAULT_PARAMS}
    merged = dict(org.settings_json or {})
    merged.update(clean)
    org.settings_json = merged
    version = RuleParamVersion(
        id=new_id(),
        org_id=org.id,
        effective_from=effective_from,
        params=clean,
        created_by=actor_id,
    )
    db.add(version)
    db.flush()
    return version


# ---------------------------------------------------------------------------
# Exception upsert
# ---------------------------------------------------------------------------


def raise_exception(
    db: Session,
    org_id: str,
    user_id: str,
    day: date,
    type_: str,
    detail: str,
    params: dict | None = None,
) -> AttendanceException:
    severity, blocking = EXCEPTION_META.get(type_, ("warning", False))
    existing = db.scalar(
        select(AttendanceException).where(
            AttendanceException.user_id == user_id,
            AttendanceException.day == day,
            AttendanceException.type == type_,
        )
    )
    if existing:
        if existing.status == "cleared":
            existing.status = "open"
            existing.resolved_at = None
            existing.resolved_by = None
        existing.detail = detail
        existing.severity = severity
        existing.blocking = blocking
        existing.rule_params = params or {}
        return existing
    record = AttendanceException(
        id=new_id(),
        org_id=org_id,
        user_id=user_id,
        day=day,
        type=type_,
        severity=severity,
        blocking=blocking,
        detail=detail,
        status="open",
        rule_params=params or {},
    )
    db.add(record)
    db.flush()
    return record


def clear_exception(db: Session, user_id: str, day: date, type_: str) -> None:
    """US-07 AC-4: an exception is never deleted, it is retained in history."""
    existing = db.scalar(
        select(AttendanceException).where(
            AttendanceException.user_id == user_id,
            AttendanceException.day == day,
            AttendanceException.type == type_,
        )
    )
    if existing and existing.status == "open":
        existing.status = "cleared"
        existing.resolved_at = utcnow()
        existing.resolution_note = "Condition no longer present at re-evaluation."


def _toggle(db, org_id, user_id, day, type_, condition, detail, params=None):
    if condition:
        raise_exception(db, org_id, user_id, day, type_, detail, params)
    else:
        clear_exception(db, user_id, day, type_)


# ---------------------------------------------------------------------------
# Day-level evaluation (on write and nightly)
# ---------------------------------------------------------------------------


def evaluate_day(
    db: Session, org: Organisation, user: User, day: date, now: datetime | None = None
) -> list[AttendanceException]:
    now = now or T.utcnow()
    params = rule_params(db, org, day)
    figures = calc.compute_day(db, org, user, day, now=now)
    tzname = calc.user_timezone(db, org, user)
    window = T.local_day_bounds(day, tzname)
    day_is_past = window[1] <= now

    sessions = db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.user_id == user.id,
            AttendanceSession.superseded_by.is_(None),
            AttendanceSession.start_at < window[1],
            (AttendanceSession.end_at.is_(None))
            | (AttendanceSession.end_at > window[0]),
        )
    ).all()

    # --- Open session / missing clock-out ---------------------------------
    open_sessions = [s for s in sessions if s.end_at is None]
    _toggle(
        db, org.id, user.id, day, "MISSING_CLOCK_OUT",
        bool(open_sessions) and day_is_past,
        "An attendance session on this day has no clock-out.",
    )
    _toggle(
        db, org.id, user.id, day, "OPEN_SESSION",
        bool(open_sessions) and not day_is_past,
        "An attendance session is still running.",
    )

    # --- FR-C-09 runaway session ------------------------------------------
    max_minutes = (org.max_session_hours or 12) * 60
    runaway = any(
        ((s.end_at or now) - s.start_at).total_seconds() / 60 > max_minutes
        for s in sessions
    )
    _toggle(
        db, org.id, user.id, day, "LONG_SESSION", runaway,
        f"A session exceeded the configured maximum of {org.max_session_hours} hours.",
        {"max_session_hours": org.max_session_hours},
    )

    # --- Auto-stopped entries need confirmation (FR-C-09) -----------------
    unconfirmed = any(s.system_generated and not s.confirmed for s in sessions)
    _toggle(
        db, org.id, user.id, day, "UNCONFIRMED_AUTO_STOP", unconfirmed,
        "A system-generated clock-out requires your confirmation.",
    )

    # --- Overlap (section 12.3) -------------------------------------------
    intervals = sorted(
        (s.start_at, s.end_at or now) for s in sessions if (s.end_at or now) > s.start_at
    )
    overlapping = any(
        intervals[i][1] > intervals[i + 1][0] for i in range(len(intervals) - 1)
    )
    _toggle(
        db, org.id, user.id, day, "OVERLAP", overlapping,
        "Two attendance sessions overlap in time.",
    )

    # --- BR-06 unexplained absence ----------------------------------------
    unexplained = (
        day_is_past
        and figures.expected > 0
        and figures.present == 0
        and figures.absence == 0
    )
    _toggle(
        db, org.id, user.id, day, "UNEXPLAINED_ABSENCE", unexplained,
        f"{figures.expected // 60}h expected but no attendance and no approved absence.",
    )

    # --- WT-04 break shortfall --------------------------------------------
    break_required_after = params["wt04_break_after_minutes"]
    min_break = params["wt04_min_break_minutes"]
    breaks_taken = figures.break_paid + figures.break_unpaid
    shortfall = (
        figures.present > break_required_after
        and breaks_taken < min_break
        and (day_is_past or not figures.open_session)
    )
    _toggle(
        db, org.id, user.id, day, "BREAK_SHORTFALL", shortfall,
        f"{breaks_taken} min of break recorded; {min_break} min required after "
        f"{break_required_after // 60} h worked.",
        {"wt04_break_after_minutes": break_required_after,
         "wt04_min_break_minutes": min_break},
    )

    # --- Daily maximum -----------------------------------------------------
    daily_max = params["daily_max_minutes"]
    _toggle(
        db, org.id, user.id, day, "DAILY_MAX_EXCEEDED", figures.present > daily_max,
        f"{figures.present} min of presence exceeds the daily maximum of {daily_max} min.",
        {"daily_max_minutes": daily_max},
    )

    # --- WT-02 minimum daily rest -----------------------------------------
    min_rest = params["wt02_min_daily_rest_minutes"]
    rest_breach = False
    rest_detail = ""
    if sessions:
        earliest = min(s.start_at for s in sessions)
        previous_end = db.scalar(
            select(AttendanceSession.end_at)
            .where(
                AttendanceSession.user_id == user.id,
                AttendanceSession.superseded_by.is_(None),
                AttendanceSession.end_at.is_not(None),
                AttendanceSession.end_at <= earliest,
            )
            .order_by(AttendanceSession.end_at.desc())
        )
        if previous_end is not None:
            gap = int((earliest - previous_end).total_seconds() / 60)
            if gap < min_rest:
                rest_breach = True
                rest_detail = (
                    f"Only {gap // 60}h {gap % 60:02d}m rest since the previous shift; "
                    f"{min_rest // 60}h required."
                )
    _toggle(
        db, org.id, user.id, day, "MIN_REST", rest_breach, rest_detail,
        {"wt02_min_daily_rest_minutes": min_rest},
    )

    # --- FR-G-04 overtime awaiting approval -------------------------------
    rule = calc.get_overtime_rule(db, org.id)
    aggregate = db.scalar(
        select(DayAggregate).where(
            DayAggregate.user_id == user.id, DayAggregate.day == day
        )
    )
    unapproved = 0
    if rule.requires_prior_approval and aggregate:
        total = (
            aggregate.overtime_standard + aggregate.overtime_night
            + aggregate.overtime_weekend + aggregate.overtime_holiday
        )
        unapproved = max(0, total - aggregate.overtime_approved_minutes)
    _toggle(
        db, org.id, user.id, day, "UNAPPROVED_OVERTIME", unapproved > 0,
        f"{unapproved} min of overtime has not been approved and is excluded "
        "from the payroll export.",
    )

    db.flush()
    return db.scalars(
        select(AttendanceException).where(
            AttendanceException.user_id == user.id,
            AttendanceException.day == day,
            AttendanceException.status == "open",
        )
    ).all()


# ---------------------------------------------------------------------------
# Rolling-window rules (nightly batch)
# ---------------------------------------------------------------------------


def evaluate_rolling(db: Session, org: Organisation, user: User, day: date) -> None:
    """WT-01 (average weekly hours over a reference period), WT-03 (weekly
    rest) and WT-05 (night work limit)."""
    params = rule_params(db, org, day)

    # WT-01 -----------------------------------------------------------------
    months = params["wt01_reference_months"]
    window_start = day - timedelta(days=int(months * 30.44))
    rows = db.scalars(
        select(DayAggregate).where(
            DayAggregate.user_id == user.id,
            DayAggregate.day >= window_start,
            DayAggregate.day <= day,
        )
    ).all()
    # The average is taken over the part of the reference period the employee
    # was actually in scope for; otherwise a new joiner, or an employee whose
    # records start mid-window, would always appear compliant.
    effective_start = window_start
    if user.employment_start and user.employment_start > effective_start:
        effective_start = user.employment_start
    if rows:
        earliest = min(r.day for r in rows)
        if earliest > effective_start:
            effective_start = earliest
    weeks = max(1.0, ((day - effective_start).days + 1) / 7)
    total_worked = sum(r.net_worked_minutes for r in rows)
    average = total_worked / weeks
    limit = params["wt01_max_avg_weekly_minutes"]
    opted_out = user.wt_optout_from is not None and user.wt_optout_from <= day
    _toggle(
        db, org.id, user.id, day, "MAX_WEEKLY_AVERAGE",
        average > limit and not opted_out,
        f"Average of {average / 60:.1f} h/week over {months} months exceeds the "
        f"{limit / 60:.0f} h limit.",
        {"wt01_max_avg_weekly_minutes": limit, "wt01_reference_months": months},
    )

    # WT-05 -----------------------------------------------------------------
    night_months = params["wt05_reference_months"]
    night_start = day - timedelta(days=int(night_months * 30.44))
    night_rows = [r for r in rows if r.day >= night_start]
    worked_days = [r for r in night_rows if r.net_worked_minutes > 0]
    night_days = [r for r in worked_days if r.night_minutes > 0]
    night_average = (
        sum(r.night_minutes for r in worked_days) / len(worked_days)
        if worked_days else 0
    )
    is_night_worker = len(night_days) >= max(1, len(worked_days) // 3)
    _toggle(
        db, org.id, user.id, day, "NIGHT_WORK_LIMIT",
        is_night_worker and night_average > params["wt05_night_avg_max_minutes"],
        f"Average night work of {night_average / 60:.1f} h per working day exceeds "
        f"{params['wt05_night_avg_max_minutes'] / 60:.0f} h.",
    )

    # WT-03 weekly rest ------------------------------------------------------
    week_start, week_end = T.iso_week_bounds(day, org.week_start)
    tzname = calc.user_timezone(db, org, user)
    window = (
        T.local_day_bounds(week_start, tzname)[0],
        T.local_day_bounds(week_end, tzname)[1],
    )
    sessions = db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.user_id == user.id,
            AttendanceSession.superseded_by.is_(None),
            AttendanceSession.start_at < window[1],
            AttendanceSession.end_at.is_not(None),
            AttendanceSession.end_at > window[0],
        )
    ).all()
    worked_intervals = T.normalise(
        [T.clip((s.start_at, s.end_at), window) for s in sessions if s.end_at]  # type: ignore[arg-type]
    )
    worked_intervals = [i for i in worked_intervals if i]
    gaps = T.subtract([window], worked_intervals)
    longest_rest = max((T.total_minutes([g]) for g in gaps), default=0)
    required = params["wt03_min_weekly_rest_minutes"] + params["wt02_min_daily_rest_minutes"]
    _toggle(
        db, org.id, user.id, week_end, "WEEKLY_REST",
        bool(sessions) and longest_rest < required,
        f"Longest continuous rest in the week was {longest_rest / 60:.1f} h; "
        f"{required / 60:.0f} h required.",
        {"wt03_min_weekly_rest_minutes": params["wt03_min_weekly_rest_minutes"]},
    )
    db.flush()


def blocking_exceptions(
    db: Session, user_id: str, start: date, end: date
) -> list[AttendanceException]:
    """FR-H-04: submission is blocked while these remain unresolved."""
    return list(
        db.scalars(
            select(AttendanceException).where(
                AttendanceException.user_id == user_id,
                AttendanceException.day >= start,
                AttendanceException.day <= end,
                AttendanceException.status == "open",
                AttendanceException.blocking.is_(True),
            )
        ).all()
    )


def refresh(
    db: Session, org: Organisation, user: User, start: date, end: date
) -> None:
    """Recompute aggregates and re-evaluate day rules for a range."""
    calc.recompute_range(db, org, user, start, end)
    for day in T.daterange(start, end):
        evaluate_day(db, org, user, day)
