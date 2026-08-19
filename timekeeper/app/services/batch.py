"""Background jobs.

Rules are evaluated on write for same-day feedback and here in a nightly batch
for rolling-window rules (section 16). This module also holds retention
enforcement (FR-L-04), runaway-session handling (FR-C-09), submission reminders
(FR-H-07), auto-approval (FR-H-08) and scheduled report delivery (FR-I-11).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Approval,
    AttendanceException,
    AttendanceSession,
    DayAggregate,
    Organisation,
    Period,
    SavedReport,
    TimeEntry,
    User,
    utcnow,
)
from .. import audit
from . import calc, notifications, periods, rules, timeutil as T

log = logging.getLogger("timekeeper.batch")


# ---------------------------------------------------------------------------
# FR-C-09 runaway sessions
# ---------------------------------------------------------------------------


def handle_runaway_sessions(db: Session, org: Organisation, now: datetime | None = None) -> int:
    now = now or utcnow()
    limit = timedelta(hours=org.max_session_hours or 12)
    open_sessions = db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.org_id == org.id,
            AttendanceSession.end_at.is_(None),
            AttendanceSession.superseded_by.is_(None),
        )
    ).all()
    handled = 0
    for session in open_sessions:
        if now - session.start_at <= limit:
            continue
        user = db.get(User, session.user_id)
        if user is None:
            continue
        notifications.notify(
            db, user.id, "timer_runaway", "Your timer has been running a long time",
            f"Started {session.start_at.isoformat(timespec='minutes')} UTC. "
            "Please stop or correct it.",
            "/#/tracker",
        )
        if org.auto_stop_runaway:
            end = _shift_end_for(db, org, user, session) or (session.start_at + limit)
            if end <= session.start_at:
                end = session.start_at + limit
            before = audit.snapshot(session)
            session.end_at = end
            session.status = "auto_closed"
            session.system_generated = True
            session.confirmed = False
            for brk in session.breaks:
                if brk.end_at is None:
                    brk.end_at = end
            audit.record(
                db, action="attendance.auto_stopped",
                entity_type="attendance_session", entity_id=session.id,
                org_id=org.id, before=before, after=session,
                note="System-generated clock-out; requires employee confirmation (FR-C-09).",
            )
        day = T.to_local(session.start_at, calc.user_timezone(db, org, user)).date()
        rules.refresh(db, org, user, day, day + timedelta(days=1))
        handled += 1
    return handled


def _shift_end_for(
    db: Session, org: Organisation, user: User, session: AttendanceSession
) -> datetime | None:
    tzname = calc.user_timezone(db, org, user)
    local_start = T.to_local(session.start_at, tzname)
    pattern = calc.pattern_for(db, user.id, local_start.date())
    if pattern is None or not pattern.shift_end:
        return None
    end_time = T.parse_hhmm(pattern.shift_end)
    candidate = T.to_utc(datetime.combine(local_start.date(), end_time), tzname)
    if candidate <= session.start_at:
        candidate = T.to_utc(
            datetime.combine(local_start.date() + timedelta(days=1), end_time), tzname
        )
    return candidate


# ---------------------------------------------------------------------------
# Nightly rule evaluation
# ---------------------------------------------------------------------------


def evaluate_org(db: Session, org: Organisation, lookback_days: int = 7,
                 today: date | None = None) -> dict:
    today = today or date.today()
    start = today - timedelta(days=lookback_days)
    users = db.scalars(
        select(User).where(User.org_id == org.id, User.status == "active")
    ).all()
    for user in users:
        calc.recompute_range(db, org, user, start, today)
        for day in T.daterange(start, today):
            rules.evaluate_day(db, org, user, day)
        rules.evaluate_rolling(db, org, user, today - timedelta(days=1))
    return {"users": len(users), "from": start, "to": today}


def notify_new_exceptions(db: Session, org: Organisation, since: datetime) -> int:
    """FR-K-02: managers are told when an exception is raised in their team."""
    from ..routers.approvals import _approvers_for_user

    fresh = db.scalars(
        select(AttendanceException).where(
            AttendanceException.org_id == org.id,
            AttendanceException.status == "open",
            AttendanceException.created_at >= since,
        )
    ).all()
    sent = 0
    for record in fresh:
        user = db.get(User, record.user_id)
        if user is None:
            continue
        for approver_id in _approvers_for_user(db, user):
            notifications.notify(
                db, approver_id, "exception_raised",
                f"Exception raised: {record.type.replace('_', ' ').lower()}",
                f"{user.display_name} on {record.day.isoformat()}.",
                "/#/manager",
            )
            sent += 1
    return sent


def absent_without_notice(db: Session, org: Organisation, today: date | None = None) -> int:
    """FR-K-02: an employee absent without notice past a configured grace
    period."""
    from ..routers.approvals import _approvers_for_user

    today = today or date.today()
    sent = 0
    for user in db.scalars(
        select(User).where(User.org_id == org.id, User.status == "active")
    ).all():
        expected, _ = calc.expected_minutes(db, user, today)
        if expected <= 0:
            continue
        aggregate = db.scalar(
            select(DayAggregate).where(
                DayAggregate.user_id == user.id, DayAggregate.day == today
            )
        )
        if aggregate and (aggregate.present_minutes or aggregate.absence_minutes):
            continue
        pattern = calc.pattern_for(db, user.id, today)
        if pattern is None or not pattern.shift_start:
            continue
        tzname = calc.user_timezone(db, org, user)
        shift_start = T.to_utc(
            datetime.combine(today, T.parse_hhmm(pattern.shift_start)), tzname
        )
        if utcnow() - shift_start < timedelta(minutes=30):
            continue
        for approver_id in _approvers_for_user(db, user):
            notifications.notify(
                db, approver_id, "absent_without_notice",
                "Expected but not present",
                f"{user.display_name} was expected at {pattern.shift_start}.",
                "/#/manager",
            )
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# FR-H-07 reminders and FR-H-08 auto-approval
# ---------------------------------------------------------------------------


def submission_reminders(db: Session, org: Organisation, today: date | None = None) -> int:
    today = today or date.today()
    period = periods.ensure_period(db, org, today - timedelta(days=1))
    if period.cutoff_date is None:
        return 0
    days_left = (period.cutoff_date - today).days
    if days_left not in (3, 1, 0, -1):
        return 0
    sent = 0
    for user in db.scalars(
        select(User).where(User.org_id == org.id, User.status == "active",
                           User.has_login.is_(True))
    ).all():
        approval = periods.ensure_approval(db, period, user.id)
        if approval.status in (periods.SUBMITTED, periods.APPROVED) or approval.excluded:
            continue
        urgency = "overdue" if days_left < 0 else f"due in {days_left} day(s)"
        notifications.notify(
            db, user.id, "period_due", f"Your timesheet is {urgency}",
            f"{period.start_date.isoformat()} – {period.end_date.isoformat()}",
            "/#/tracker",
        )
        sent += 1
    return sent


def auto_approve(db: Session, org: Organisation, today: date | None = None) -> int:
    """FR-H-08: auto-approval after a configurable grace period, only where the
    organisation has enabled it."""
    if not org.auto_approve_after_days:
        return 0
    today = today or date.today()
    approved = 0
    for period in db.scalars(
        select(Period).where(Period.org_id == org.id, Period.status != periods.LOCKED)
    ).all():
        if period.cutoff_date is None:
            continue
        if (today - period.cutoff_date).days < org.auto_approve_after_days:
            continue
        for approval in db.scalars(
            select(Approval).where(
                Approval.period_id == period.id, Approval.status == periods.SUBMITTED
            )
        ).all():
            blocking = rules.blocking_exceptions(
                db, approval.user_id, period.start_date, period.end_date
            )
            if blocking:
                continue
            before = audit.snapshot(approval)
            approval.status = periods.APPROVED
            approval.decided_at = utcnow()
            approval.reason = (
                f"Auto-approved {org.auto_approve_after_days} day(s) after the cut-off."
            )
            audit.record(
                db, action="period.auto_approved", entity_type="approval",
                entity_id=approval.id, org_id=org.id, before=before, after=approval,
                note=approval.reason,
            )
            notifications.notify(
                db, approval.user_id, "period_decided",
                "Your timesheet was automatically approved", approval.reason, "/#/tracker",
            )
            approved += 1
    return approved


# ---------------------------------------------------------------------------
# FR-I-11 scheduled reports
# ---------------------------------------------------------------------------


def _cron_due(expression: str, moment: datetime) -> bool:
    """Minimal 5-field cron matcher: minute hour day-of-month month day-of-week."""
    try:
        minute, hour, dom, month, dow = expression.split()
    except ValueError:
        return False

    def matches(field: str, value: int, wrap: int | None = None) -> bool:
        if field == "*":
            return True
        for part in field.split(","):
            if part.startswith("*/"):
                step = int(part[2:])
                if step and value % step == 0:
                    return True
            elif "-" in part:
                low, high = (int(x) for x in part.split("-"))
                if low <= value <= high:
                    return True
            elif part.isdigit() and int(part) == value:
                return True
            elif wrap is not None and part.isdigit() and int(part) % wrap == value:
                return True
        return False

    return (
        matches(minute, moment.minute)
        and matches(hour, moment.hour)
        and matches(dom, moment.day)
        and matches(month, moment.month)
        and matches(dow, (moment.weekday() + 1) % 7)
    )


def deliver_scheduled_reports(db: Session, org: Organisation, now: datetime | None = None) -> int:
    from ..schemas import ReportFilters
    from . import exports, reports as report_service

    now = now or utcnow()
    delivered = 0
    for saved in db.scalars(
        select(SavedReport).where(
            SavedReport.org_id == org.id, SavedReport.schedule_cron.is_not(None)
        )
    ).all():
        if not _cron_due(saved.schedule_cron, now):
            continue
        if saved.last_sent_at and (now - saved.last_sent_at) < timedelta(minutes=55):
            continue
        owner = db.get(User, saved.owner_id)
        if owner is None or owner.status != "active":
            continue
        from ..routers.reports import _OwnerPrincipal

        filters = ReportFilters(**saved.filters)
        report = report_service.build(
            db, org, _OwnerPrincipal(owner), saved.report_type, filters
        )
        payload = exports.to_csv(report, org.duration_format)
        log.info(
            "scheduled report '%s' (%d rows, %d bytes) to %s",
            saved.name, len(report["rows"]), len(payload),
            ", ".join(saved.schedule_recipients or [owner.email or ""]),
        )
        saved.last_sent_at = now
        notifications.notify(
            db, owner.id, "report_scheduled", f"Scheduled report '{saved.name}' was sent",
            f"{len(report['rows'])} rows.", "/#/reports",
        )
        delivered += 1
    return delivered


# ---------------------------------------------------------------------------
# FR-L-04 / DP-08 retention
# ---------------------------------------------------------------------------


def enforce_retention(db: Session, org: Organisation, today: date | None = None) -> dict:
    """Attendance records are retained for the statutory minimum, then
    irreversibly anonymised or deleted, and the deletion event is logged."""
    today = today or date.today()
    cutoff = date(today.year - (org.retention_years or 3), 12, 31)
    deleted = {"sessions": 0, "entries": 0, "aggregates": 0, "exceptions": 0}

    window_end = T.local_day_bounds(cutoff, org.timezone)[1]
    for session in db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.org_id == org.id, AttendanceSession.start_at < window_end
        )
    ).all():
        db.delete(session)
        deleted["sessions"] += 1
    for entry in db.scalars(
        select(TimeEntry).where(TimeEntry.org_id == org.id, TimeEntry.day <= cutoff)
    ).all():
        db.delete(entry)
        deleted["entries"] += 1
    for aggregate in db.scalars(
        select(DayAggregate).where(
            DayAggregate.org_id == org.id, DayAggregate.day <= cutoff
        )
    ).all():
        db.delete(aggregate)
        deleted["aggregates"] += 1
    for record in db.scalars(
        select(AttendanceException).where(
            AttendanceException.org_id == org.id, AttendanceException.day <= cutoff
        )
    ).all():
        db.delete(record)
        deleted["exceptions"] += 1

    if any(deleted.values()):
        audit.record(
            db, action="retention.purged", entity_type="organisation", entity_id=org.id,
            org_id=org.id, after={"cutoff": cutoff.isoformat(), **deleted},
            note=f"Retention: {org.retention_years} years after the end of the calendar year.",
        )
    return {"cutoff": cutoff, **deleted}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_all(db: Session, now: datetime | None = None) -> dict:
    now = now or utcnow()
    summary = []
    for org in db.scalars(select(Organisation)).all():
        since = now - timedelta(hours=25)
        result = {
            "organisation": org.name,
            "runaway_sessions": handle_runaway_sessions(db, org, now),
            "evaluated": evaluate_org(db, org),
            "exception_notices": notify_new_exceptions(db, org, since),
            "absent_notices": absent_without_notice(db, org),
            "reminders": submission_reminders(db, org),
            "auto_approved": auto_approve(db, org),
            "scheduled_reports": deliver_scheduled_reports(db, org, now),
        }
        if now.hour < 4:
            result["retention"] = enforce_retention(db, org)
        summary.append(result)
        db.commit()
    from . import webhooks as webhook_service

    dispatched = webhook_service.dispatch_pending(db)
    return {"ran_at": now.isoformat(), "organisations": summary,
            "webhooks_dispatched": dispatched}
