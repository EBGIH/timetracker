"""Worked-example regression suite for the calculation engine.

NFR-M-03 requires a regression suite of worked examples signed off by HR and
Payroll. Each test below states the example in plain language first, so the
sign-off can be done against the description rather than the code.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from app.models import AbsenceRequest, Holiday, OvertimeApproval, new_id
from app.services import calc, timeutil as T
from conftest import TZ, add_session, make_user

MONDAY = date(2026, 6, 1)      # a Monday
SATURDAY = date(2026, 6, 6)
SUNDAY = date(2026, 6, 7)


# ---------------------------------------------------------------------------
# BR-01 Net worked time = present − unpaid breaks; paid breaks count as worked
# ---------------------------------------------------------------------------


def test_br01_unpaid_break_is_deducted(db, org, employee):
    """08:00–16:30 with a 30-minute unpaid lunch = 8:00 net."""
    add_session(db, org, employee, MONDAY, "08:00", "16:30",
                breaks=[("12:00", "12:30", False)])
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.present == 510
    assert figures.break_unpaid == 30
    assert figures.net == 480


def test_br01_paid_break_counts_as_worked(db, org, employee):
    """08:00–16:00 with a 15-minute paid rest = 8:00 net, not 7:45."""
    add_session(db, org, employee, MONDAY, "08:00", "16:00",
                breaks=[("10:00", "10:15", True)])
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.break_paid == 15
    assert figures.break_unpaid == 0
    assert figures.net == 480


def test_multiple_breaks_of_both_kinds(db, org, employee):
    add_session(db, org, employee, MONDAY, "06:00", "16:00",
                breaks=[("09:00", "09:15", True), ("12:00", "12:45", False)])
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.present == 600
    assert figures.break_paid == 15
    assert figures.break_unpaid == 45
    assert figures.net == 555


# ---------------------------------------------------------------------------
# BR-02 Daily balance = net worked + paid absence − expected
# ---------------------------------------------------------------------------


def test_br02_balance_surplus_and_deficit(db, org, employee):
    add_session(db, org, employee, MONDAY, "08:00", "17:00")  # 9:00 against 8:00
    assert calc.compute_day(db, org, employee, MONDAY).balance == 60

    add_session(db, org, employee, MONDAY + timedelta(days=1), "08:00", "14:00")
    assert calc.compute_day(db, org, employee, MONDAY + timedelta(days=1)).balance == -120


def test_br02_paid_absence_fills_the_day(db, org, employee, annual_policy):
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=MONDAY, end_date=MONDAY, status="approved", deducted_minutes=480,
    ))
    db.flush()
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.absence == 480
    assert figures.absence_paid == 480
    assert figures.balance == 0


def test_unpaid_absence_does_not_fill_the_balance(db, org, employee, annual_policy):
    annual_policy.is_paid = False
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=MONDAY, end_date=MONDAY, status="approved", deducted_minutes=480,
    ))
    db.flush()
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.absence == 480
    assert figures.absence_paid == 0
    assert figures.balance == -480


def test_half_day_absence(db, org, employee, annual_policy):
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=MONDAY, end_date=MONDAY, part_day_hours=4.0, status="approved",
        deducted_minutes=240,
    ))
    add_session(db, org, employee, MONDAY, "08:00", "12:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.absence == 240
    assert figures.net == 240
    assert figures.balance == 0


# ---------------------------------------------------------------------------
# BR-03 / FR-G-03 Overtime on net worked time, split into categories
# ---------------------------------------------------------------------------


def test_br03_overtime_is_on_net_not_gross(db, org, employee):
    """09:00–18:00 (9:00 gross) with a 60-minute unpaid break is exactly the
    expected 8 hours — there is no overtime, even though presence was nine."""
    add_session(db, org, employee, MONDAY, "09:00", "18:00",
                breaks=[("12:00", "13:00", False)])
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.present == 540
    assert figures.net == 480
    assert figures.overtime_total == 0


def test_overtime_standard(db, org, employee):
    add_session(db, org, employee, MONDAY, "08:00", "18:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.ot_standard == 120
    assert figures.ot_night == 0


def test_br04_night_hours_and_night_overtime(db, org, employee):
    """14:00–00:00 on Monday: ten hours worked, of which the two after 22:00
    are night hours. The two hours of overtime are the last two, so both fall
    into the night category rather than the standard one."""
    add_session(db, org, employee, MONDAY, "14:00", "00:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.net == 600
    assert figures.night == 120
    assert figures.ot_night == 120
    assert figures.ot_standard == 0


def test_night_hours_after_midnight_belong_to_the_next_day(db, org, employee):
    """A shift crossing midnight is attributed to each local day it touches,
    and the night window is evaluated per day."""
    add_session(db, org, employee, MONDAY, "22:00", "06:00")
    monday = calc.compute_day(db, org, employee, MONDAY)
    tuesday = calc.compute_day(db, org, employee, MONDAY + timedelta(days=1))
    assert (monday.night, tuesday.night) == (120, 360)


def test_weekend_work_is_weekend_overtime(db, org, employee):
    add_session(db, org, employee, SATURDAY, "08:00", "12:00")
    figures = calc.compute_day(db, org, employee, SATURDAY)
    assert figures.expected == 0
    assert figures.ot_weekend == 240
    assert figures.ot_standard == 0


def test_br05_public_holiday_work_is_holiday_overtime(db, org, employee):
    db.add(Holiday(id=new_id(), org_id=org.id, day=MONDAY, name="Test holiday"))
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "16:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.is_holiday is True
    assert figures.expected == 0
    assert figures.ot_holiday == 480
    assert figures.ot_standard == 0


def test_holiday_override_makes_it_a_working_day(db, org, employee):
    db.add(Holiday(id=new_id(), org_id=org.id, day=MONDAY, name="Working holiday",
                   is_working_day_override=True))
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "16:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.expected == 480
    assert figures.ot_holiday == 0


def test_categories_are_mutually_exclusive(db, org, employee):
    """The four buckets must sum to total overtime; payroll must never be able
    to multiply the same minute twice."""
    add_session(db, org, employee, MONDAY, "14:00", "03:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    total = figures.ot_standard + figures.ot_night + figures.ot_weekend + figures.ot_holiday
    assert total == max(0, figures.net - figures.expected)


def test_daily_threshold_overrides_expected(db, org, employee):
    rule = calc.get_overtime_rule(db, org.id)
    rule.daily_threshold_minutes = 420  # 7 hours
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "16:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.ot_standard == 60


def test_weekly_threshold_adds_to_the_last_worked_day(db, org, employee):
    rule = calc.get_overtime_rule(db, org.id)
    rule.weekly_threshold_minutes = 2100  # 35 hours
    db.flush()
    for offset in range(5):
        add_session(db, org, employee, MONDAY + timedelta(days=offset), "08:00", "16:00")
    calc.recompute_range(db, org, employee, MONDAY, MONDAY + timedelta(days=6))
    totals = calc.period_totals(db, employee.id, MONDAY, MONDAY + timedelta(days=6))
    assert totals["net_worked_minutes"] == 2400
    assert totals["overtime_total"] == 300


# ---------------------------------------------------------------------------
# FR-G-04 / BR-10 Approved overtime only
# ---------------------------------------------------------------------------


def test_unapproved_overtime_is_excluded_until_approved(db, org, employee):
    rule = calc.get_overtime_rule(db, org.id)
    rule.requires_prior_approval = True
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "18:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.ot_standard == 120
    assert figures.ot_approved == 0

    db.add(OvertimeApproval(id=new_id(), org_id=org.id, user_id=employee.id,
                            day=MONDAY, minutes=120, status="approved"))
    db.flush()
    assert calc.compute_day(db, org, employee, MONDAY).ot_approved == 120


# ---------------------------------------------------------------------------
# Overnight shifts, day boundaries and part-time patterns
# ---------------------------------------------------------------------------


def test_overnight_shift_is_split_across_local_days(db, org, employee):
    add_session(db, org, employee, MONDAY, "22:00", "06:00")
    monday = calc.compute_day(db, org, employee, MONDAY)
    tuesday = calc.compute_day(db, org, employee, MONDAY + timedelta(days=1))
    assert monday.present == 120
    assert tuesday.present == 360
    assert monday.night == 120
    assert tuesday.night == 360


def test_part_time_pattern_changes_expected_hours(db, org):
    part_timer = make_user(db, org, "PT", "Part", "Timer",
                           pattern=[300, 300, 300, 300, 0, 0, 0])
    assert calc.expected_minutes(db, part_timer, MONDAY)[0] == 300
    assert calc.expected_minutes(db, part_timer, MONDAY + timedelta(days=4))[0] == 0


def test_fr_b05_recalculation_uses_the_pattern_in_force(db, org, employee):
    """A pattern effective from a later date must not change earlier days."""
    from app.models import WorkingPattern

    old = calc.pattern_for(db, employee.id, MONDAY)
    old.valid_to = MONDAY
    db.add(WorkingPattern(
        id=new_id(), user_id=employee.id, valid_from=MONDAY + timedelta(days=1),
        contracted_hours_per_week=20.0, expected_minutes=[240] * 5 + [0, 0],
    ))
    db.flush()
    assert calc.expected_minutes(db, employee, MONDAY)[0] == 480
    assert calc.expected_minutes(db, employee, MONDAY + timedelta(days=1))[0] == 240


def test_no_expected_hours_before_employment_start(db, org):
    joiner = make_user(db, org, "J", "Late", "Joiner", start=MONDAY + timedelta(days=2))
    assert calc.expected_minutes(db, joiner, MONDAY)[0] == 0
    assert calc.expected_minutes(db, joiner, MONDAY + timedelta(days=2))[0] == 480


def test_no_expected_hours_after_employment_end(db, org):
    leaver = make_user(db, org, "L", "Early", "Leaver")
    leaver.employment_end = MONDAY
    db.flush()
    assert calc.expected_minutes(db, leaver, MONDAY)[0] == 480
    assert calc.expected_minutes(db, leaver, MONDAY + timedelta(days=1))[0] == 0


# ---------------------------------------------------------------------------
# FR-E-03 automatic break deduction
# ---------------------------------------------------------------------------


def test_automatic_break_deduction(db, org, employee):
    org.auto_break_after_minutes = 360
    org.auto_break_minutes = 30
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "16:00")
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.break_unpaid == 30
    assert figures.net == 450


def test_automatic_deduction_tops_up_a_short_break(db, org, employee):
    org.auto_break_after_minutes = 360
    org.auto_break_minutes = 30
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "16:00",
                breaks=[("12:00", "12:10", False)])
    figures = calc.compute_day(db, org, employee, MONDAY)
    assert figures.break_unpaid == 30


def test_automatic_deduction_not_applied_below_the_threshold(db, org, employee):
    org.auto_break_after_minutes = 360
    org.auto_break_minutes = 30
    db.flush()
    add_session(db, org, employee, MONDAY, "08:00", "12:00")
    assert calc.compute_day(db, org, employee, MONDAY).break_unpaid == 0


# ---------------------------------------------------------------------------
# Running sessions
# ---------------------------------------------------------------------------


def test_running_session_counts_up_to_now(db, org, employee):
    now_local = datetime.combine(MONDAY, time(12, 0))
    add_session(db, org, employee, MONDAY, "08:00", None)
    figures = calc.compute_day(db, org, employee, MONDAY, now=T.to_utc(now_local, TZ))
    assert figures.present == 240
    assert figures.open_session is True


# ---------------------------------------------------------------------------
# FR-A-09 / BR-07 rounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minute,second,step,direction,expected",
    [
        (7, 0, 15, "nearest", 0),
        (8, 0, 15, "nearest", 15),
        (7, 30, 15, "nearest", 15),
        (7, 0, 15, "up", 15),
        (7, 0, 15, "down", 0),
        (2, 0, 5, "nearest", 0),
        (3, 0, 5, "nearest", 5),
        (0, 0, 5, "up", 0),
        (0, 1, 5, "up", 5),
    ],
)
def test_rounding(minute, second, step, direction, expected):
    moment = datetime(2026, 6, 1, 8, minute, second)
    assert T.round_timestamp(moment, step, direction).minute == expected


def test_br07_rounding_is_symmetric_by_default(db, org, employee):
    """"Nearest" must not systematically favour the employer: an early arrival
    and a late departure round in the same direction."""
    org.rounding_minutes = 15
    org.rounding_direction = "nearest"
    start = T.round_timestamp(datetime(2026, 6, 1, 7, 52), 15, "nearest")
    end = T.round_timestamp(datetime(2026, 6, 1, 16, 8), 15, "nearest")
    assert start.strftime("%H:%M") == "07:45"
    assert end.strftime("%H:%M") == "16:15"


def test_rounding_disabled_leaves_the_timestamp_alone():
    moment = datetime(2026, 6, 1, 8, 7, 33)
    assert T.round_timestamp(moment, 0, "nearest") == moment


# ---------------------------------------------------------------------------
# Interval algebra
# ---------------------------------------------------------------------------


def test_interval_subtract_and_intersect():
    base = [(datetime(2026, 6, 1, 8), datetime(2026, 6, 1, 16))]
    cut = [(datetime(2026, 6, 1, 12), datetime(2026, 6, 1, 12, 30))]
    result = T.subtract(base, cut)
    assert T.total_minutes(result) == 450
    assert len(result) == 2
    assert T.total_minutes(T.intersect(base, cut)) == 30


def test_take_last_minutes():
    intervals = [
        (datetime(2026, 6, 1, 8), datetime(2026, 6, 1, 12)),
        (datetime(2026, 6, 1, 13), datetime(2026, 6, 1, 17)),
    ]
    trailing = T.take_last_minutes(intervals, 120)
    assert T.total_minutes(trailing) == 120
    assert trailing[0][0] == datetime(2026, 6, 1, 15)


def test_period_bounds():
    assert T.period_bounds("monthly", date(2026, 6, 15)) == (date(2026, 6, 1), date(2026, 6, 30))
    assert T.period_bounds("semimonthly", date(2026, 6, 15)) == (date(2026, 6, 1), date(2026, 6, 15))
    assert T.period_bounds("semimonthly", date(2026, 6, 16)) == (date(2026, 6, 16), date(2026, 6, 30))
    assert T.period_bounds("weekly", date(2026, 6, 3)) == (date(2026, 6, 1), date(2026, 6, 7))
    start, end = T.period_bounds("biweekly", date(2026, 6, 3))
    assert (end - start).days == 13


def test_format_duration():
    assert T.format_duration(495, "hm") == "8:15"
    assert T.format_duration(495, "decimal") == "8.25"
    assert T.format_duration(-90, "hm") == "-1:30"


def test_naive_utc_applies_the_offset():
    from datetime import timezone

    aware = datetime(2026, 6, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert T.naive_utc(aware) == datetime(2026, 6, 1, 10, 0)


# ---------------------------------------------------------------------------
# Aggregate persistence and period roll-up
# ---------------------------------------------------------------------------


def test_period_totals_sum_the_days(db, org, employee):
    for offset in range(5):
        add_session(db, org, employee, MONDAY + timedelta(days=offset), "08:00", "16:30",
                    breaks=[("12:00", "12:30", False)])
    calc.recompute_range(db, org, employee, MONDAY, MONDAY + timedelta(days=6))
    totals = calc.period_totals(db, employee.id, MONDAY, MONDAY + timedelta(days=6))
    assert totals["net_worked_minutes"] == 2400
    assert totals["expected_minutes"] == 2400
    assert totals["balance_minutes"] == 0
    assert totals["break_unpaid_minutes"] == 150


def test_recompute_is_idempotent(db, org, employee):
    add_session(db, org, employee, MONDAY, "08:00", "16:00")
    first = calc.persist_day(db, org, employee, MONDAY)
    net = first.net_worked_minutes
    for _ in range(3):
        again = calc.persist_day(db, org, employee, MONDAY)
    assert again.id == first.id
    assert again.net_worked_minutes == net
