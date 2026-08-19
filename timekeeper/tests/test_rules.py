"""Working-time rules engine (specification section 16) and exception
detection."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.models import AbsenceRequest, AttendanceException, new_id
from app.services import calc, rules, timeutil as T
from conftest import TZ, add_session

MONDAY = date(2026, 6, 1)
LAST_MONDAY = date(2026, 5, 4)


def open_types(db, user_id, day=None):
    query = [AttendanceException.user_id == user_id, AttendanceException.status == "open"]
    if day:
        query.append(AttendanceException.day == day)
    from sqlalchemy import select

    return {row.type for row in db.scalars(select(AttendanceException).where(*query)).all()}


# ---------------------------------------------------------------------------
# WT-04 rest break (US-07 AC-1)
# ---------------------------------------------------------------------------


def test_wt04_break_shortfall_is_raised(db, org, employee):
    """More than six hours worked with no recorded break raises a shortfall."""
    add_session(db, org, employee, LAST_MONDAY, "08:00", "16:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "BREAK_SHORTFALL" in open_types(db, employee.id, LAST_MONDAY)


def test_wt04_no_shortfall_when_the_break_is_long_enough(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "08:00", "16:30",
                breaks=[("12:00", "12:30", False)])
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "BREAK_SHORTFALL" not in open_types(db, employee.id, LAST_MONDAY)


def test_wt04_threshold_is_configurable(db, org, employee):
    rules.save_rule_params(db, org, {"wt04_break_after_minutes": 300,
                                     "wt04_min_break_minutes": 45},
                           date(2020, 1, 1), employee.id)
    add_session(db, org, employee, LAST_MONDAY, "08:00", "14:00",
                breaks=[("11:00", "11:30", False)])
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "BREAK_SHORTFALL" in open_types(db, employee.id, LAST_MONDAY)


def test_rule_parameters_are_versioned(db, org, employee):
    rules.save_rule_params(db, org, {"wt02_min_daily_rest_minutes": 720},
                           date(2026, 6, 1), employee.id)
    assert rules.rule_params(db, org, date(2026, 5, 1))["wt02_min_daily_rest_minutes"] == 660
    assert rules.rule_params(db, org, date(2026, 6, 2))["wt02_min_daily_rest_minutes"] == 720


# ---------------------------------------------------------------------------
# WT-02 minimum daily rest (US-07 AC-2)
# ---------------------------------------------------------------------------


def test_wt02_min_rest_breach(db, org, employee):
    """Finishing at 22:00 and starting again at 05:00 is seven hours of rest."""
    add_session(db, org, employee, LAST_MONDAY, "14:00", "22:00")
    add_session(db, org, employee, LAST_MONDAY + timedelta(days=1), "05:00", "13:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY + timedelta(days=1))
    assert "MIN_REST" in open_types(db, employee.id, LAST_MONDAY + timedelta(days=1))


def test_wt02_no_breach_with_eleven_hours(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "06:00", "14:00")
    add_session(db, org, employee, LAST_MONDAY + timedelta(days=1), "06:00", "14:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY + timedelta(days=1))
    assert "MIN_REST" not in open_types(db, employee.id, LAST_MONDAY + timedelta(days=1))


# ---------------------------------------------------------------------------
# WT-01 maximum average weekly working time (US-07 AC-3)
# ---------------------------------------------------------------------------


def test_wt01_average_weekly_breach(db, org, employee):
    """Six 11-hour days a week over four weeks averages 66 h — well past 48."""
    start = date(2026, 5, 4)
    for week in range(4):
        for offset in range(6):
            day = start + timedelta(weeks=week, days=offset)
            add_session(db, org, employee, day, "06:00", "17:00")
    end = start + timedelta(weeks=4)
    calc.recompute_range(db, org, employee, start, end)
    rules.evaluate_rolling(db, org, employee, end)
    assert "MAX_WEEKLY_AVERAGE" in open_types(db, employee.id, end)


def test_wt01_individual_opt_out_suppresses_the_breach(db, org, employee):
    start = date(2026, 5, 4)
    for week in range(4):
        for offset in range(6):
            add_session(db, org, employee, start + timedelta(weeks=week, days=offset),
                        "06:00", "17:00")
    employee.wt_optout_from = date(2026, 1, 1)
    employee.wt_optout_ref = "Signed agreement 2026/17"
    db.flush()
    end = start + timedelta(weeks=4)
    calc.recompute_range(db, org, employee, start, end)
    rules.evaluate_rolling(db, org, employee, end)
    assert "MAX_WEEKLY_AVERAGE" not in open_types(db, employee.id, end)


def test_wt01_normal_hours_do_not_breach(db, org, employee):
    start = date(2026, 5, 4)
    for week in range(4):
        for offset in range(5):
            add_session(db, org, employee, start + timedelta(weeks=week, days=offset),
                        "08:00", "16:30", breaks=[("12:00", "12:30", False)])
    end = start + timedelta(weeks=4)
    calc.recompute_range(db, org, employee, start, end)
    rules.evaluate_rolling(db, org, employee, end)
    assert "MAX_WEEKLY_AVERAGE" not in open_types(db, employee.id, end)


# ---------------------------------------------------------------------------
# WT-03 weekly rest
# ---------------------------------------------------------------------------


def test_wt03_weekly_rest_breach(db, org, employee):
    """Working every day of the week leaves no 35-hour continuous rest."""
    for offset in range(7):
        add_session(db, org, employee, LAST_MONDAY + timedelta(days=offset), "06:00", "20:00")
    rules.evaluate_rolling(db, org, employee, LAST_MONDAY + timedelta(days=6))
    week_end = LAST_MONDAY + timedelta(days=6)
    assert "WEEKLY_REST" in open_types(db, employee.id, week_end)


# ---------------------------------------------------------------------------
# Operational exceptions
# ---------------------------------------------------------------------------


def test_missing_clock_out_on_a_past_day(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "08:00", None)
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    found = open_types(db, employee.id, LAST_MONDAY)
    assert "MISSING_CLOCK_OUT" in found
    assert "OPEN_SESSION" not in found


def test_br06_unexplained_absence(db, org, employee):
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "UNEXPLAINED_ABSENCE" in open_types(db, employee.id, LAST_MONDAY)


def test_fr_f05_approved_absence_suppresses_unexplained_absence(
    db, org, employee, annual_policy
):
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=LAST_MONDAY, end_date=LAST_MONDAY, status="approved",
        deducted_minutes=480,
    ))
    db.flush()
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "UNEXPLAINED_ABSENCE" not in open_types(db, employee.id, LAST_MONDAY)


def test_weekend_does_not_produce_unexplained_absence(db, org, employee):
    saturday = LAST_MONDAY + timedelta(days=5)
    rules.evaluate_day(db, org, employee, saturday)
    assert "UNEXPLAINED_ABSENCE" not in open_types(db, employee.id, saturday)


def test_long_session_exception(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "06:00", "23:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "LONG_SESSION" in open_types(db, employee.id, LAST_MONDAY)


def test_daily_maximum(db, org, employee):
    rules.save_rule_params(db, org, {"daily_max_minutes": 600}, date(2020, 1, 1), employee.id)
    add_session(db, org, employee, LAST_MONDAY, "06:00", "17:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "DAILY_MAX_EXCEEDED" in open_types(db, employee.id, LAST_MONDAY)


def test_us07_ac4_resolved_exceptions_are_retained_not_deleted(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "08:00", "16:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    from sqlalchemy import select

    record = db.scalar(select(AttendanceException).where(
        AttendanceException.user_id == employee.id,
        AttendanceException.type == "BREAK_SHORTFALL"))
    record.status = "resolved"
    record.resolution_note = "Break was taken but not recorded; corrected."
    record.resolved_by = employee.id
    db.flush()
    # Re-evaluating must not silently drop the history.
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    still_there = db.scalar(select(AttendanceException).where(
        AttendanceException.id == record.id))
    assert still_there is not None
    assert still_there.resolution_note


def test_exception_clears_when_the_condition_goes_away(db, org, employee, break_types):
    add_session(db, org, employee, LAST_MONDAY, "08:00", "16:00")
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "BREAK_SHORTFALL" in open_types(db, employee.id, LAST_MONDAY)

    from sqlalchemy import select

    from app.models import BreakRecord

    session = db.scalar(select(__import__("app.models", fromlist=["AttendanceSession"])
                               .AttendanceSession))
    lunch_start = T.to_utc(datetime.combine(LAST_MONDAY, time(12, 0)), TZ)
    db.add(BreakRecord(id=new_id(), session_id=session.id, start_at=lunch_start,
                       end_at=lunch_start + timedelta(minutes=45), is_paid=False))
    db.flush()
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    assert "BREAK_SHORTFALL" not in open_types(db, employee.id, LAST_MONDAY)


def test_blocking_exceptions_are_reported_for_submission(db, org, employee):
    add_session(db, org, employee, LAST_MONDAY, "08:00", None)
    rules.evaluate_day(db, org, employee, LAST_MONDAY)
    blocking = rules.blocking_exceptions(db, employee.id, LAST_MONDAY, LAST_MONDAY)
    assert any(item.type == "MISSING_CLOCK_OUT" for item in blocking)


def test_break_shortfall_is_not_blocking(db, org, employee):
    """A compliance breach must be visible and reportable, but it must not stop
    the employee submitting their period — the employer resolves it."""
    assert rules.EXCEPTION_META["BREAK_SHORTFALL"][1] is False
    assert rules.EXCEPTION_META["MISSING_CLOCK_OUT"][1] is True
