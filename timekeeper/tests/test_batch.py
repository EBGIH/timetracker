"""Background jobs: runaway sessions, reminders, auto-approval, scheduled
reports and retention (FR-C-09, FR-H-07, FR-H-08, FR-I-11, FR-L-04)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AttendanceException,
    AttendanceSession,
    Notification,
    SavedReport,
    Team,
    new_id,
)
from app.services import batch, calc, periods, rules, timeutil as T
from conftest import TZ, add_session, make_user

YESTERDAY = date.today() - timedelta(days=1)


# ---------------------------------------------------------------------------
# FR-C-09 runaway sessions
# ---------------------------------------------------------------------------


def test_runaway_session_notifies_the_employee(db, org, employee):
    add_session(db, org, employee, date.today() - timedelta(days=1), "06:00", None)
    handled = batch.handle_runaway_sessions(db, org)
    assert handled == 1
    notes = db.scalars(select(Notification).where(
        Notification.user_id == employee.id,
        Notification.type == "timer_runaway")).all()
    assert notes


def test_auto_stop_marks_the_entry_system_generated_and_unconfirmed(db, org, employee):
    org.auto_stop_runaway = True
    org.max_session_hours = 12
    db.flush()
    add_session(db, org, employee, date.today() - timedelta(days=1), "06:00", None)
    batch.handle_runaway_sessions(db, org)

    session = db.scalar(select(AttendanceSession).where(
        AttendanceSession.user_id == employee.id))
    assert session.end_at is not None
    assert session.status == "auto_closed"
    assert session.system_generated is True
    assert session.confirmed is False

    open_types = {row.type for row in db.scalars(select(AttendanceException).where(
        AttendanceException.user_id == employee.id,
        AttendanceException.status == "open")).all()}
    assert "UNCONFIRMED_AUTO_STOP" in open_types


def test_auto_stop_uses_the_configured_shift_end(db, org, employee):
    org.auto_stop_runaway = True
    db.flush()
    pattern = calc.pattern_for(db, employee.id, date.today())
    pattern.shift_start = "06:00"
    pattern.shift_end = "14:00"
    db.flush()

    day = date.today() - timedelta(days=2)
    add_session(db, org, employee, day, "06:00", None)
    batch.handle_runaway_sessions(db, org)
    session = db.scalar(select(AttendanceSession).where(
        AttendanceSession.user_id == employee.id))
    local_end = T.to_local(session.end_at, TZ)
    assert local_end.strftime("%H:%M") == "14:00"


def test_a_session_within_the_limit_is_left_alone(db, org, employee):
    org.auto_stop_runaway = True
    db.flush()
    now = T.utcnow()
    start_local = T.to_local(now, TZ) - timedelta(hours=2)
    db.add(AttendanceSession(
        id=new_id(), org_id=org.id, user_id=employee.id,
        start_at=T.to_utc(start_local.replace(tzinfo=None), TZ),
        source="timer", status="open", created_by=employee.id))
    db.flush()
    assert batch.handle_runaway_sessions(db, org) == 0


# ---------------------------------------------------------------------------
# FR-H-07 reminders
# ---------------------------------------------------------------------------


def test_submission_reminder_is_sent_near_the_cut_off(db, org, employee):
    period = periods.ensure_period(db, org, YESTERDAY)
    period.cutoff_date = date.today() + timedelta(days=1)
    db.flush()
    sent = batch.submission_reminders(db, org, date.today())
    assert sent >= 1
    notes = db.scalars(select(Notification).where(
        Notification.user_id == employee.id, Notification.type == "period_due")).all()
    assert notes
    assert "due in 1 day" in notes[0].title


def test_no_reminder_once_the_period_is_submitted(db, org, employee):
    period = periods.ensure_period(db, org, YESTERDAY)
    period.cutoff_date = date.today() + timedelta(days=1)
    approval = periods.ensure_approval(db, period, employee.id)
    approval.status = periods.SUBMITTED
    db.flush()
    assert batch.submission_reminders(db, org, date.today()) == 0


def test_no_reminder_far_from_the_cut_off(db, org, employee):
    period = periods.ensure_period(db, org, YESTERDAY)
    period.cutoff_date = date.today() + timedelta(days=9)
    db.flush()
    assert batch.submission_reminders(db, org, date.today()) == 0


# ---------------------------------------------------------------------------
# FR-H-08 auto-approval
# ---------------------------------------------------------------------------


def test_auto_approval_only_when_enabled(db, org, employee):
    period = periods.ensure_period(db, org, YESTERDAY)
    period.cutoff_date = date.today() - timedelta(days=5)
    approval = periods.ensure_approval(db, period, employee.id)
    approval.status = periods.SUBMITTED
    db.flush()
    assert batch.auto_approve(db, org, date.today()) == 0

    org.auto_approve_after_days = 3
    db.flush()
    assert batch.auto_approve(db, org, date.today()) == 1
    assert approval.status == periods.APPROVED
    assert "Auto-approved" in approval.reason


def test_auto_approval_skips_blocking_exceptions(db, org, employee):
    org.auto_approve_after_days = 3
    period = periods.ensure_period(db, org, YESTERDAY)
    period.cutoff_date = date.today() - timedelta(days=5)
    approval = periods.ensure_approval(db, period, employee.id)
    approval.status = periods.SUBMITTED
    db.flush()
    rules.raise_exception(db, org.id, employee.id, YESTERDAY, "MISSING_CLOCK_OUT",
                          "No clock-out")
    db.flush()
    assert batch.auto_approve(db, org, date.today()) == 0
    assert approval.status == periods.SUBMITTED


# ---------------------------------------------------------------------------
# FR-K-02 manager notices
# ---------------------------------------------------------------------------


def test_manager_is_told_when_an_exception_is_raised_in_the_team(db, org):
    team = Team(id=new_id(), org_id=org.id, name="Line")
    db.add(team)
    db.flush()
    manager = make_user(db, org, "M", "Mia", "Manager", role="manager", team_id=team.id)
    worker = make_user(db, org, "W", "Will", "Worker", team_id=team.id)
    team.manager_user_id = manager.id
    db.flush()

    rules.raise_exception(db, org.id, worker.id, YESTERDAY, "BREAK_SHORTFALL",
                          "No break recorded")
    db.flush()
    sent = batch.notify_new_exceptions(db, org, T.utcnow() - timedelta(hours=1))
    assert sent >= 1
    notes = db.scalars(select(Notification).where(
        Notification.user_id == manager.id,
        Notification.type == "exception_raised")).all()
    assert notes


def test_absent_without_notice_alerts_the_manager(db, org):
    team = Team(id=new_id(), org_id=org.id, name="Line")
    db.add(team)
    db.flush()
    manager = make_user(db, org, "M2", "Max", "Manager", role="manager", team_id=team.id)
    worker = make_user(db, org, "W4", "Wendy", "Worker", team_id=team.id)
    team.manager_user_id = manager.id
    pattern = calc.pattern_for(db, worker.id, date.today())
    pattern.shift_start = "00:01"
    pattern.shift_end = "08:00"
    db.flush()

    if date.today().weekday() >= 5:
        pytest.skip("no expected hours at the weekend")
    calc.recompute_range(db, org, worker, date.today(), date.today())
    sent = batch.absent_without_notice(db, org, date.today())
    assert sent >= 1


# ---------------------------------------------------------------------------
# FR-I-11 scheduled reports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expression,moment,expected", [
    ("0 6 * * *", datetime(2026, 6, 1, 6, 0), True),
    ("0 6 * * *", datetime(2026, 6, 1, 7, 0), False),
    ("30 6 1 * *", datetime(2026, 6, 1, 6, 30), True),
    ("30 6 1 * *", datetime(2026, 6, 2, 6, 30), False),
    ("0 6 * * 1", datetime(2026, 6, 1, 6, 0), True),   # Monday
    ("0 6 * * 1", datetime(2026, 6, 2, 6, 0), False),
    ("0 */4 * * *", datetime(2026, 6, 1, 8, 0), True),
    ("0 6 1-5 * *", datetime(2026, 6, 3, 6, 0), True),
    ("not a cron", datetime(2026, 6, 1, 6, 0), False),
])
def test_cron_matcher(expression, moment, expected):
    assert batch._cron_due(expression, moment) is expected


def test_a_due_scheduled_report_is_delivered_once(db, org, employee):
    saved = SavedReport(
        id=new_id(), org_id=org.id, owner_id=employee.id, name="Daily attendance",
        report_type="attendance",
        filters={"start": date.today().isoformat(), "end": date.today().isoformat()},
        schedule_cron="0 6 * * *", schedule_recipients=["payroll@example.com"],
    )
    db.add(saved)
    db.flush()
    moment = datetime(2026, 6, 1, 6, 0)
    assert batch.deliver_scheduled_reports(db, org, moment) == 1
    # A second run in the same hour must not send it again.
    assert batch.deliver_scheduled_reports(db, org, moment + timedelta(minutes=5)) == 0
    assert saved.last_sent_at is not None


# ---------------------------------------------------------------------------
# FR-L-04 retention
# ---------------------------------------------------------------------------


def test_retention_keeps_recent_records_and_removes_old_ones(db, org, employee):
    org.retention_years = 3
    db.flush()
    old_day = date(date.today().year - 5, 3, 2)
    recent_day = date.today() - timedelta(days=30)
    add_session(db, org, employee, old_day, "08:00", "16:00")
    add_session(db, org, employee, recent_day, "08:00", "16:00")
    calc.recompute_range(db, org, employee, recent_day, recent_day)
    db.flush()

    result = batch.enforce_retention(db, org, date.today())
    assert result["sessions"] == 1
    remaining = db.scalars(select(AttendanceSession).where(
        AttendanceSession.user_id == employee.id)).all()
    assert len(remaining) == 1
    assert T.to_local(remaining[0].start_at, TZ).date() == recent_day


def test_evaluate_org_produces_aggregates_and_exceptions(db, org, employee):
    add_session(db, org, employee, YESTERDAY, "08:00", "18:00")
    result = batch.evaluate_org(db, org, lookback_days=3, today=date.today())
    assert result["users"] == 1
    totals = calc.period_totals(db, employee.id, YESTERDAY, YESTERDAY)
    assert totals["net_worked_minutes"] == 600
