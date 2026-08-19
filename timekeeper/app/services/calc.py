"""The calculation engine — business rules BR-01 .. BR-12.

This module is deliberately free of HTTP concerns so that it can be exercised
directly by the worked-example regression suite required by NFR-M-03.

Category priority for overtime buckets is holiday > weekend > night >
standard, so that the four buckets sum exactly to total overtime and payroll
never multiplies the same minute twice. Night minutes worked are additionally
reported in their own informational field (BR-04).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AbsencePolicy,
    AbsenceRequest,
    AttendanceSession,
    BreakType,
    DayAggregate,
    Holiday,
    Location,
    Organisation,
    OvertimeApproval,
    OvertimeRule,
    User,
    WorkingPattern,
    new_id,
)
from . import timeutil as T


# ---------------------------------------------------------------------------
# Configuration lookups
# ---------------------------------------------------------------------------


def get_overtime_rule(db: Session, org_id: str) -> OvertimeRule:
    rule = db.scalar(
        select(OvertimeRule).where(
            OvertimeRule.org_id == org_id, OvertimeRule.is_default.is_(True)
        )
    )
    if rule is None:
        rule = OvertimeRule(id=new_id(), org_id=org_id)
        db.add(rule)
        db.flush()
    return rule


def user_timezone(db: Session, org: Organisation, user: User) -> str:
    if user.location_id:
        location = db.get(Location, user.location_id)
        if location and location.timezone:
            return location.timezone
    return org.timezone


def pattern_for(db: Session, user_id: str, day: date) -> WorkingPattern | None:
    """FR-B-05: the pattern that was effective on that date."""
    return db.scalar(
        select(WorkingPattern)
        .where(
            WorkingPattern.user_id == user_id,
            WorkingPattern.valid_from <= day,
            (WorkingPattern.valid_to.is_(None)) | (WorkingPattern.valid_to >= day),
        )
        .order_by(WorkingPattern.valid_from.desc())
    )


def holiday_for(db: Session, org_id: str, location_id: str | None, day: date) -> Holiday | None:
    rows = db.scalars(
        select(Holiday).where(Holiday.org_id == org_id, Holiday.day == day)
    ).all()
    specific = [h for h in rows if h.location_id == location_id]
    if specific:
        return specific[0]
    generic = [h for h in rows if h.location_id is None]
    return generic[0] if generic else None


def expected_minutes(db: Session, user: User, day: date) -> tuple[int, bool]:
    """Expected minutes from the working pattern, and whether the day is a
    public holiday. BR-05: public holidays are non-working days by default."""
    holiday = holiday_for(db, user.org_id, user.location_id, day)
    is_holiday = holiday is not None and not holiday.is_working_day_override
    if user.employment_start and day < user.employment_start:
        return 0, is_holiday
    if user.employment_end and day > user.employment_end:
        return 0, is_holiday
    pattern = pattern_for(db, user.id, day)
    if pattern is None:
        return 0, is_holiday
    if is_holiday:
        return 0, True
    values = list(pattern.expected_minutes or [])
    while len(values) < 7:
        values.append(0)
    return int(values[day.weekday()] or 0), is_holiday


# ---------------------------------------------------------------------------
# Absence contribution
# ---------------------------------------------------------------------------


def absence_minutes_for_day(
    db: Session, user: User, day: date, expected: int
) -> tuple[int, int, list[AbsenceRequest]]:
    """Returns (total absence minutes, paid absence minutes, requests).

    US-05 AC-2: days with no expected hours (weekend, public holiday) are
    excluded from the deduction, which falls out of using `expected` here.
    """
    requests = db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.user_id == user.id,
            AbsenceRequest.status == "approved",
            AbsenceRequest.start_date <= day,
            AbsenceRequest.end_date >= day,
        )
    ).all()
    if not requests or expected <= 0:
        return 0, 0, list(requests)
    total = 0
    paid = 0
    for request in requests:
        if request.part_day_hours and request.start_date == request.end_date:
            minutes = min(expected, int(round(request.part_day_hours * 60)))
        else:
            minutes = expected
        total += minutes
        policy = db.get(AbsencePolicy, request.policy_id)
        if policy and policy.is_paid:
            paid += minutes
    total = min(total, expected)
    paid = min(paid, expected)
    return total, paid, list(requests)


# ---------------------------------------------------------------------------
# The day aggregate
# ---------------------------------------------------------------------------


class DayFigures:
    """Plain result object so the engine can be unit-tested without the ORM."""

    __slots__ = (
        "day", "expected", "present", "break_paid", "break_unpaid", "net",
        "night", "absence", "absence_paid", "ot_standard", "ot_night",
        "ot_weekend", "ot_holiday", "ot_approved", "balance", "first_in",
        "last_out", "is_holiday", "worked_intervals", "open_session",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @property
    def overtime_total(self) -> int:
        return self.ot_standard + self.ot_night + self.ot_weekend + self.ot_holiday

    def as_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__ if slot != "worked_intervals"}


def compute_day(
    db: Session, org: Organisation, user: User, day: date, now: datetime | None = None
) -> DayFigures:
    now = now or T.utcnow()
    tzname = user_timezone(db, org, user)
    window = T.local_day_bounds(day, tzname)
    rule = get_overtime_rule(db, org.id)

    sessions = db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.user_id == user.id,
            AttendanceSession.superseded_by.is_(None),
            AttendanceSession.start_at < window[1],
            (AttendanceSession.end_at.is_(None))
            | (AttendanceSession.end_at > window[0]),
        )
    ).all()

    present: list[T.Interval] = []
    paid_break: list[T.Interval] = []
    unpaid_break: list[T.Interval] = []
    open_session = False
    first_in: datetime | None = None
    last_out: datetime | None = None

    for session in sessions:
        end = session.end_at
        if end is None:
            open_session = True
            end = min(now, window[1])
        piece = T.clip((session.start_at, end), window)
        if piece is None:
            continue
        present.append(piece)
        if first_in is None or piece[0] < first_in:
            first_in = piece[0]
        if session.end_at is not None and (last_out is None or piece[1] > last_out):
            last_out = piece[1]
        for brk in session.breaks:
            brk_end = brk.end_at or min(now, end)
            brk_piece = T.clip((brk.start_at, brk_end), piece)
            if brk_piece is None:
                continue
            is_paid = brk.is_paid
            if brk.break_type_id:
                btype = db.get(BreakType, brk.break_type_id)
                if btype is not None:
                    is_paid = btype.is_paid
            (paid_break if is_paid else unpaid_break).append(brk_piece)

    present = T.normalise(present)
    present_minutes = T.total_minutes(present)
    paid_break_minutes = T.total_minutes(T.intersect(paid_break, present))
    unpaid_break_intervals = T.intersect(unpaid_break, present)
    unpaid_break_minutes = T.total_minutes(unpaid_break_intervals)

    # FR-E-03 automatic break deduction
    auto_after = org.auto_break_after_minutes or 0
    auto_minutes = org.auto_break_minutes or 0
    if auto_after and auto_minutes and present_minutes >= auto_after:
        shortfall = auto_minutes - unpaid_break_minutes
        if shortfall > 0:
            unpaid_break_minutes += shortfall
            trailing = T.take_last_minutes(T.subtract(present, unpaid_break_intervals), shortfall)
            unpaid_break_intervals = T.normalise(unpaid_break_intervals + trailing)

    worked = T.subtract(present, unpaid_break_intervals)
    net = present_minutes - unpaid_break_minutes  # BR-01
    if net < 0:
        net = 0

    nights = T.night_windows(window, tzname, rule.night_start, rule.night_end)
    night_minutes = T.total_minutes(T.intersect(worked, nights))  # BR-04

    expected, is_holiday = expected_minutes(db, user, day)
    absence, absence_paid, _ = absence_minutes_for_day(db, user, day, expected)

    # --- Overtime classification (FR-G-03, BR-03, BR-05) ------------------
    ot_standard = ot_night = ot_weekend = ot_holiday = 0
    if net > 0:
        if is_holiday:
            ot_holiday = net
        else:
            threshold = expected
            if rule.daily_threshold_minutes:
                threshold = (
                    min(expected, rule.daily_threshold_minutes) if expected else 0
                )
            overtime = max(0, net - threshold)
            if overtime:
                if day.weekday() in (rule.weekend_days or []):
                    ot_weekend = overtime
                else:
                    ot_intervals = T.take_last_minutes(worked, overtime)
                    ot_night = T.total_minutes(T.intersect(ot_intervals, nights))
                    ot_standard = overtime - ot_night

    overtime_total = ot_standard + ot_night + ot_weekend + ot_holiday
    if rule.requires_prior_approval:
        approved = db.scalar(
            select(OvertimeApproval).where(
                OvertimeApproval.user_id == user.id,
                OvertimeApproval.day == day,
                OvertimeApproval.status == "approved",
            )
        )
        ot_approved = min(overtime_total, approved.minutes) if approved else 0
    else:
        ot_approved = overtime_total

    balance = net + absence_paid - expected  # BR-02

    return DayFigures(
        day=day,
        expected=expected,
        present=present_minutes,
        break_paid=paid_break_minutes,
        break_unpaid=unpaid_break_minutes,
        net=net,
        night=night_minutes,
        absence=absence,
        absence_paid=absence_paid,
        ot_standard=ot_standard,
        ot_night=ot_night,
        ot_weekend=ot_weekend,
        ot_holiday=ot_holiday,
        ot_approved=ot_approved,
        balance=balance,
        first_in=first_in,
        last_out=last_out,
        is_holiday=is_holiday,
        worked_intervals=worked,
        open_session=open_session,
    )


def persist_day(db: Session, org: Organisation, user: User, day: date) -> DayAggregate:
    figures = compute_day(db, org, user, day)
    aggregate = db.scalar(
        select(DayAggregate).where(
            DayAggregate.user_id == user.id, DayAggregate.day == day
        )
    )
    if aggregate is None:
        aggregate = DayAggregate(id=new_id(), org_id=org.id, user_id=user.id, day=day)
        db.add(aggregate)
    aggregate.expected_minutes = figures.expected
    aggregate.present_minutes = figures.present
    aggregate.break_paid_minutes = figures.break_paid
    aggregate.break_unpaid_minutes = figures.break_unpaid
    aggregate.net_worked_minutes = figures.net
    aggregate.night_minutes = figures.night
    aggregate.absence_minutes = figures.absence
    aggregate.absence_paid_minutes = figures.absence_paid
    aggregate.overtime_standard = figures.ot_standard
    aggregate.overtime_night = figures.ot_night
    aggregate.overtime_weekend = figures.ot_weekend
    aggregate.overtime_holiday = figures.ot_holiday
    aggregate.overtime_approved_minutes = figures.ot_approved
    aggregate.balance_minutes = figures.balance
    aggregate.first_in = figures.first_in
    aggregate.last_out = figures.last_out
    aggregate.is_holiday = bool(figures.is_holiday)
    db.flush()
    return aggregate


def recompute_range(
    db: Session, org: Organisation, user: User, start: date, end: date
) -> list[DayAggregate]:
    aggregates = [persist_day(db, org, user, day) for day in T.daterange(start, end)]
    weeks = {T.iso_week_bounds(day, org.week_start)[0] for day in T.daterange(start, end)}
    for week_start in weeks:
        apply_weekly_overtime(db, org, user, week_start)
    return aggregates


def apply_weekly_overtime(
    db: Session, org: Organisation, user: User, week_start: date
) -> None:
    """FR-G-02 weekly threshold. Any weekly surplus not already captured as
    daily overtime is added to the last worked day of the week as standard
    overtime, so the four buckets remain mutually exclusive."""
    rule = get_overtime_rule(db, org.id)
    if not rule.weekly_threshold_minutes:
        return
    week_end = week_start + timedelta(days=6)
    rows = db.scalars(
        select(DayAggregate)
        .where(
            DayAggregate.user_id == user.id,
            DayAggregate.day >= week_start,
            DayAggregate.day <= week_end,
        )
        .order_by(DayAggregate.day)
    ).all()
    if not rows:
        return
    weekly_net = sum(r.net_worked_minutes for r in rows)
    daily_ot = sum(
        r.overtime_standard + r.overtime_night + r.overtime_weekend + r.overtime_holiday
        for r in rows
    )
    extra = weekly_net - rule.weekly_threshold_minutes - daily_ot
    if extra <= 0:
        return
    target = None
    for row in reversed(rows):
        if row.net_worked_minutes > 0:
            target = row
            break
    if target is None:
        return
    target.overtime_standard += extra
    rule_needs_approval = rule.requires_prior_approval
    if not rule_needs_approval:
        target.overtime_approved_minutes += extra
    db.flush()


# ---------------------------------------------------------------------------
# Period roll-up
# ---------------------------------------------------------------------------


def period_totals(db: Session, user_id: str, start: date, end: date) -> dict:
    rows = db.scalars(
        select(DayAggregate)
        .where(
            DayAggregate.user_id == user_id,
            DayAggregate.day >= start,
            DayAggregate.day <= end,
        )
        .order_by(DayAggregate.day)
    ).all()
    totals = {
        "expected_minutes": 0,
        "present_minutes": 0,
        "break_paid_minutes": 0,
        "break_unpaid_minutes": 0,
        "net_worked_minutes": 0,
        "night_minutes": 0,
        "absence_minutes": 0,
        "absence_paid_minutes": 0,
        "overtime_standard": 0,
        "overtime_night": 0,
        "overtime_weekend": 0,
        "overtime_holiday": 0,
        "overtime_approved_minutes": 0,
        "balance_minutes": 0,
        "days": len(rows),
    }
    for row in rows:
        for key in list(totals):
            if key == "days":
                continue
            totals[key] += getattr(row, key)
    totals["overtime_total"] = (
        totals["overtime_standard"]
        + totals["overtime_night"]
        + totals["overtime_weekend"]
        + totals["overtime_holiday"]
    )
    return totals
