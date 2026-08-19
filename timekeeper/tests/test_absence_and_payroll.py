"""Absence entitlement arithmetic (Module F) and the payroll layer (Module J)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import (
    AbsenceBalance,
    AbsenceRequest,
    Period,
    Team,
    new_id,
)
from app.services import absence, calc, payroll, periods
from conftest import add_session, make_user

YEAR = 2026
JANUARY = date(YEAR, 1, 5)      # a Monday
JUNE = date(YEAR, 6, 1)         # a Monday


# ---------------------------------------------------------------------------
# Entitlement and accrual
# ---------------------------------------------------------------------------


def test_a_leave_day_follows_the_working_pattern(db, org):
    full_timer = make_user(db, org, "F", "Full", "Timer")
    part_timer = make_user(db, org, "P", "Part", "Timer",
                           pattern=[300, 300, 300, 300, 0, 0, 0])
    assert absence.average_day_minutes(db, full_timer) == 480
    assert absence.average_day_minutes(db, part_timer) == 300


def test_annual_accrual_is_granted_in_full(db, org, employee, annual_policy):
    balance = absence.balance_for(db, employee, annual_policy, YEAR, date(YEAR, 1, 2))
    assert balance["entitlement_minutes"] == 25 * 480
    assert balance["accrued_minutes"] == 25 * 480


def test_monthly_accrual_builds_up_over_the_year(db, org, employee, annual_policy):
    annual_policy.accrual_method = "monthly"
    db.flush()
    march = absence.balance_for(db, employee, annual_policy, YEAR, date(YEAR, 3, 31))
    december = absence.balance_for(db, employee, annual_policy, YEAR, date(YEAR, 12, 31))
    assert march["accrued_minutes"] == pytest.approx(25 * 480 * 3 / 12, abs=1)
    assert december["accrued_minutes"] == 25 * 480


def test_monthly_accrual_is_pro_rated_for_a_mid_year_joiner(db, org, annual_policy):
    joiner = make_user(db, org, "MJ", "Mid", "Joiner", start=date(YEAR, 7, 1))
    annual_policy.accrual_method = "monthly"
    db.flush()
    balance = absence.balance_for(db, joiner, annual_policy, YEAR, date(YEAR, 12, 31))
    assert balance["accrued_minutes"] == pytest.approx(25 * 480 * 6 / 12, abs=1)


def test_an_unlimited_policy_has_no_entitlement(db, org, employee, annual_policy):
    annual_policy.accrual_method = "unlimited"
    db.flush()
    balance = absence.balance_for(db, employee, annual_policy, YEAR)
    assert balance["unlimited"] is True
    assert balance["entitlement_minutes"] == 0


def test_taken_planned_and_pending_are_reported_separately(db, org, employee, annual_policy):
    today = date(YEAR, 6, 15)
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=date(YEAR, 3, 2), end_date=date(YEAR, 3, 3), status="approved",
        deducted_minutes=960))
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=date(YEAR, 8, 3), end_date=date(YEAR, 8, 4), status="approved",
        deducted_minutes=960))
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=date(YEAR, 9, 7), end_date=date(YEAR, 9, 7), status="pending",
        deducted_minutes=480))
    db.flush()
    balance = absence.balance_for(db, employee, annual_policy, YEAR, today)
    assert balance["taken_minutes"] == 960
    assert balance["planned_minutes"] == 960
    assert balance["pending_minutes"] == 480
    assert balance["remaining_minutes"] == 25 * 480 - 1920


def test_a_manual_adjustment_moves_the_balance(db, org, employee, annual_policy):
    db.add(AbsenceBalance(
        id=new_id(), user_id=employee.id, policy_id=annual_policy.id, year=YEAR,
        adjustment_minutes=-480, adjustment_reason="Correction from the old system"))
    db.flush()
    balance = absence.balance_for(db, employee, annual_policy, YEAR, date(YEAR, 1, 2))
    assert balance["remaining_minutes"] == 25 * 480 - 480


def test_carry_over_is_added_to_the_new_year(db, org, employee, annual_policy):
    db.add(AbsenceBalance(
        id=new_id(), user_id=employee.id, policy_id=annual_policy.id, year=YEAR,
        carried_over_minutes=5 * 480))
    db.flush()
    balance = absence.balance_for(db, employee, annual_policy, YEAR, date(YEAR, 1, 2))
    assert balance["carried_over_minutes"] == 5 * 480
    assert balance["remaining_minutes"] == 30 * 480


# ---------------------------------------------------------------------------
# Requested amount and validation
# ---------------------------------------------------------------------------


def test_requested_minutes_skips_weekends(db, org, employee):
    minutes = absence.requested_minutes(db, employee, JUNE, JUNE + timedelta(days=6), None)
    assert minutes == 5 * 480


def test_requested_minutes_for_a_part_day(db, org, employee):
    assert absence.requested_minutes(db, employee, JUNE, JUNE, 4.0) == 240
    # A part day can never exceed the expected hours for that day.
    assert absence.requested_minutes(db, employee, JUNE, JUNE, 12.0) == 480


def test_validation_blocks_a_negative_balance(db, org, employee, annual_policy):
    minutes, errors, warnings = absence.validate(
        db, employee, annual_policy, JUNE, JUNE + timedelta(days=60), None,
        today=date(YEAR, 5, 1))
    assert errors
    assert any("Insufficient balance" in e for e in errors)


def test_validation_warns_instead_when_negative_is_allowed(db, org, employee, annual_policy):
    annual_policy.allow_negative = True
    db.flush()
    minutes, errors, warnings = absence.validate(
        db, employee, annual_policy, JUNE, JUNE + timedelta(days=60), None,
        today=date(YEAR, 5, 1))
    assert not errors
    assert any("negative" in w for w in warnings)


def test_notice_period_is_enforced(db, org, employee, annual_policy):
    annual_policy.notice_days = 14
    db.flush()
    _, errors, _ = absence.validate(
        db, employee, annual_policy, JUNE, JUNE, None, today=JUNE - timedelta(days=2))
    assert any("notice" in e for e in errors)


def test_a_retrospective_request_warns_rather_than_blocks(db, org, employee, annual_policy):
    annual_policy.notice_days = 14
    db.flush()
    _, errors, warnings = absence.validate(
        db, employee, annual_policy, JUNE, JUNE, None, today=JUNE + timedelta(days=5))
    assert not errors
    assert any("Retrospective" in w for w in warnings)


def test_a_range_with_no_working_days_is_refused(db, org, employee, annual_policy):
    saturday = JUNE + timedelta(days=5)
    _, errors, _ = absence.validate(
        db, employee, annual_policy, saturday, saturday + timedelta(days=1), None,
        today=JUNE)
    assert any("no working days" in e for e in errors)


def test_fr_f04_minimum_team_coverage_warning(db, org, annual_policy):
    team = Team(id=new_id(), org_id=org.id, name="Line")
    db.add(team)
    db.flush()
    first = make_user(db, org, "C1", "First", "Member", team_id=team.id)
    second = make_user(db, org, "C2", "Second", "Member", team_id=team.id)
    annual_policy.min_team_coverage = 2
    db.flush()

    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=second.id, policy_id=annual_policy.id,
        start_date=JUNE, end_date=JUNE, status="approved", deducted_minutes=480))
    db.flush()

    warning = absence.team_coverage_warning(db, first, annual_policy, JUNE, JUNE)
    assert warning is not None
    assert "minimum coverage is 2" in warning


def test_approver_chain_falls_back_to_hr_when_there_is_no_manager(db, org, annual_policy):
    hr_user = make_user(db, org, "H", "Helen", "HR", role="hr")
    orphan = make_user(db, org, "O", "No", "Manager")
    approvers = absence.approvers_for(db, orphan, annual_policy, 0)
    assert hr_user.id in approvers


def test_time_bank_balance_is_the_sum_of_movements(db, org, employee):
    from app.models import TimeBankMovement

    db.add(TimeBankMovement(id=new_id(), user_id=employee.id, occurred_on=JUNE,
                            minutes=600, kind="accrual"))
    db.add(TimeBankMovement(id=new_id(), user_id=employee.id, occurred_on=JUNE,
                            minutes=-240, kind="time_off_in_lieu"))
    db.flush()
    assert absence.time_bank_balance(db, employee.id) == 360


# ---------------------------------------------------------------------------
# Payroll
# ---------------------------------------------------------------------------


def _period(db, org, start: date, end: date) -> Period:
    period = Period(id=new_id(), org_id=org.id, start_date=start, end_date=end,
                    status="locked")
    db.add(period)
    db.flush()
    return period


def test_payroll_rows_separate_normal_hours_from_overtime(db, org, employee):
    for offset in range(5):
        add_session(db, org, employee, JUNE + timedelta(days=offset), "08:00", "17:00",
                    breaks=[("12:00", "12:30", False)])
    calc.recompute_range(db, org, employee, JUNE, JUNE + timedelta(days=6))
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))

    rows = payroll.build_rows(db, org, period)
    row = next(r for r in rows if r["personnel_number"] == "E1")
    assert row["normal_minutes"] == 2400          # 5 × 8:00
    assert row["overtime_standard"] == 150        # 5 × 0:30
    assert row["unpaid_break_minutes"] == 150


def test_br10_unapproved_overtime_is_excluded_from_the_export(db, org, employee):
    rule = calc.get_overtime_rule(db, org.id)
    rule.requires_prior_approval = True
    db.flush()
    add_session(db, org, employee, JUNE, "08:00", "18:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))

    row = next(r for r in payroll.build_rows(db, org, period)
               if r["personnel_number"] == "E1")
    assert row["overtime_total"] == 0
    assert row["overtime_unapproved"] == 120


def test_excluded_employees_are_left_out_of_the_export(db, org, employee):
    add_session(db, org, employee, JUNE, "08:00", "16:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))
    approval = periods.ensure_approval(db, period, employee.id)
    approval.excluded = True
    approval.exclusion_reason = "On secondment"
    db.flush()
    assert payroll.build_rows(db, org, period) == []


def test_layout_controls_columns_delimiter_and_duration_format(db, org, employee):
    add_session(db, org, employee, JUNE, "08:00", "16:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))
    layout = payroll.default_layout(db, org)
    layout.columns = ["personnel_number", "normal_minutes"]
    layout.delimiter = "|"
    layout.duration_format = "hm"
    db.flush()

    content = payroll.render(payroll.build_rows(db, org, period), layout)
    lines = content.strip().splitlines()
    assert lines[0] == "personnel_number|normal_minutes"
    assert lines[1] == "E1|8:00"

    layout.duration_format = "minutes"
    assert payroll.render(payroll.build_rows(db, org, period), layout).splitlines()[1] == "E1|480"


def test_export_checksum_changes_only_when_the_content_does(db, org, employee):
    add_session(db, org, employee, JUNE, "08:00", "16:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))
    first = payroll.generate(db, org, period, employee.id)
    second = payroll.generate(db, org, period, employee.id)
    assert first.checksum == second.checksum
    assert payroll.reconcile(second, first)["changes"] == []

    add_session(db, org, employee, JUNE, "17:00", "19:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    third = payroll.generate(db, org, period, employee.id)
    assert third.checksum != second.checksum
    changes = payroll.reconcile(third, second)["changes"]
    assert changes
    assert changes[0]["personnel_number"] == "E1"


def test_reconciliation_reports_added_and_removed_employees(db, org, employee):
    add_session(db, org, employee, JUNE, "08:00", "16:00")
    calc.recompute_range(db, org, employee, JUNE, JUNE)
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))
    first = payroll.generate(db, org, period, employee.id)

    newcomer = make_user(db, org, "N1", "New", "Person")
    add_session(db, org, newcomer, JUNE, "08:00", "16:00")
    calc.recompute_range(db, org, newcomer, JUNE, JUNE)
    second = payroll.generate(db, org, period, employee.id)

    result = payroll.reconcile(second, first)
    assert "N1" in result["added"]
    assert result["removed"] == []


def test_first_export_has_nothing_to_compare_against(db, org, employee):
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))
    export = payroll.generate(db, org, period, employee.id)
    result = payroll.reconcile(export, None)
    assert result["previous_export_id"] is None
    assert "First export" in result["note"]


def test_paid_absence_is_broken_down_by_policy(db, org, employee, annual_policy):
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=employee.id, policy_id=annual_policy.id,
        start_date=JUNE, end_date=JUNE + timedelta(days=1), status="approved",
        deducted_minutes=960))
    db.flush()
    calc.recompute_range(db, org, employee, JUNE, JUNE + timedelta(days=1))
    period = _period(db, org, date(YEAR, 6, 1), date(YEAR, 6, 30))

    row = next(r for r in payroll.build_rows(db, org, period)
               if r["personnel_number"] == "E1")
    assert row["absence_paid_minutes"] == 960
    assert row["absence_al_minutes"] == 960
