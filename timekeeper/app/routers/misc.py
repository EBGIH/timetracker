"""Dashboards, notifications, audit log and privacy notice."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit as audit_service
from ..database import get_db
from ..models import (
    AbsenceRequest,
    Approval,
    AttendanceException,
    AuditRecord,
    CorrectionRequest,
    Notification,
    Organisation,
    User,
    utcnow,
)
from ..schemas import ReportFilters
from ..security import Principal, get_principal, visible_user_ids
from ..services import absence as absence_service, calc, periods, reports

dashboard = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
notifications_router = APIRouter(prefix="/api/notifications", tags=["notifications"])
audit_router = APIRouter(prefix="/api/audit", tags=["audit"])
privacy_router = APIRouter(prefix="/api/privacy", tags=["privacy"])


# ---------------------------------------------------------------------------
# Employee dashboard (section 18)
# ---------------------------------------------------------------------------


@dashboard.get("/me")
def my_dashboard(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    org = db.get(Organisation, principal.org_id)
    today = date.today()
    period = periods.ensure_period(db, org, today)
    approval = periods.ensure_approval(db, period, principal.id)
    calc.recompute_range(db, org, principal.user, period.start_date, min(today, period.end_date))
    totals = calc.period_totals(db, principal.id, period.start_date, period.end_date)

    open_exceptions = db.scalars(
        select(AttendanceException).where(
            AttendanceException.user_id == principal.id,
            AttendanceException.status == "open",
        ).order_by(AttendanceException.day.desc())
    ).all()
    pending_absence = db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.user_id == principal.id, AbsenceRequest.status == "pending"
        )
    ).all()
    db.commit()

    return {
        "period": {
            "id": period.id, "start_date": period.start_date, "end_date": period.end_date,
            "status": period.status, "approval_status": approval.status,
            "cutoff_date": period.cutoff_date,
            "days_to_cutoff": (period.cutoff_date - today).days if period.cutoff_date else None,
        },
        "totals": totals,
        "time_bank_minutes": absence_service.time_bank_balance(db, principal.id),
        "balances": absence_service.all_balances(db, principal.user),
        "exceptions": [
            {"id": e.id, "day": e.day, "type": e.type, "detail": e.detail,
             "blocking": e.blocking, "severity": e.severity}
            for e in open_exceptions
        ],
        "pending_requests": [
            {"id": r.id, "start_date": r.start_date, "end_date": r.end_date,
             "status": r.status}
            for r in pending_absence
        ],
        "unread_notifications": len(
            db.scalars(
                select(Notification.id).where(
                    Notification.user_id == principal.id,
                    Notification.channel == "inapp",
                    Notification.read_at.is_(None),
                )
            ).all()
        ),
    }


@dashboard.get("/manager")
def manager_dashboard(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    """Section 18: opens on the live team board with the exception queue
    immediately visible."""
    principal.require("view_team_attendance")
    org = db.get(Organisation, principal.org_id)
    today = date.today()
    period = periods.ensure_period(db, org, today)
    filters = ReportFilters(start=today - timedelta(days=30), end=today)
    board = reports.live_board(db, org, principal, ReportFilters(start=today, end=today))
    queue = reports.exception_queue(db, org, principal, filters)

    allowed = visible_user_ids(db, principal, "approve_timesheet")
    approval_query = select(Approval).where(
        Approval.period_id == period.id, Approval.status == periods.SUBMITTED
    )
    if allowed is not None:
        approval_query = approval_query.where(Approval.user_id.in_(allowed))
    awaiting = db.scalars(approval_query).all()

    corrections = db.scalars(
        select(CorrectionRequest).where(
            CorrectionRequest.org_id == org.id, CorrectionRequest.status == "pending"
        )
    ).all()
    absences = db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.org_id == org.id, AbsenceRequest.status == "pending"
        )
    ).all()
    if allowed is not None:
        corrections = [c for c in corrections if c.user_id in allowed]
        absences = [a for a in absences if a.user_id in allowed]
    db.commit()

    return {
        "period": {"id": period.id, "start_date": period.start_date,
                   "end_date": period.end_date, "status": period.status,
                   "cutoff_date": period.cutoff_date},
        "board": board,
        "exceptions": queue,
        "awaiting_approval": len(awaiting),
        "pending_absence": len(absences),
        "pending_corrections": len(corrections),
    }


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@notifications_router.get("")
def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(Notification).where(
        Notification.user_id == principal.id, Notification.channel == "inapp"
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    rows = db.scalars(
        query.order_by(Notification.created_at.desc()).limit(min(limit, 200))
    ).all()
    return [
        {
            "id": r.id, "type": r.type, "title": r.title, "body": r.body,
            "link": r.link, "created_at": r.created_at, "read_at": r.read_at,
        }
        for r in rows
    ]


@notifications_router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    record = db.get(Notification, notification_id)
    if record is None or record.user_id != principal.id:
        raise HTTPException(404, "Notification not found")
    record.read_at = utcnow()
    db.commit()
    return {"status": "read"}


@notifications_router.post("/read-all")
def mark_all_read(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    rows = db.scalars(
        select(Notification).where(
            Notification.user_id == principal.id, Notification.read_at.is_(None)
        )
    ).all()
    for row in rows:
        row.read_at = utcnow()
    db.commit()
    return {"marked": len(rows)}


@notifications_router.get("/catalogue")
def notification_catalogue(principal: Principal = Depends(get_principal)):
    from ..services.notifications import CATALOGUE

    return [
        {"type": key, "default_channels": list(channels), "optional": optional}
        for key, (channels, optional) in CATALOGUE.items()
    ]


# ---------------------------------------------------------------------------
# Audit log (FR-L-03)
# ---------------------------------------------------------------------------


@audit_router.get("")
def search_audit(
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
    offset: int = 0,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("view_audit")
    query = select(AuditRecord).where(AuditRecord.org_id == principal.org_id)
    if actor_id:
        query = query.where(AuditRecord.actor_user_id == actor_id)
    if entity_type:
        query = query.where(AuditRecord.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditRecord.entity_id == entity_id)
    if action:
        query = query.where(AuditRecord.action.like(f"{action}%"))
    if start:
        query = query.where(AuditRecord.occurred_at >= datetime.combine(start, datetime.min.time()))
    if end:
        query = query.where(
            AuditRecord.occurred_at <= datetime.combine(end, datetime.max.time())
        )
    rows = db.scalars(
        query.order_by(AuditRecord.occurred_at.desc()).offset(offset).limit(min(limit, 1000))
    ).all()
    names = {u.id: u.display_name for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)).all()}
    return [
        {
            "id": r.id, "occurred_at": r.occurred_at,
            "actor": names.get(r.actor_user_id, r.actor_user_id or "system"),
            "actor_role": r.actor_role, "action": r.action,
            "entity_type": r.entity_type, "entity_id": r.entity_id,
            "before": r.before_json, "after": r.after_json,
            "ip": r.ip, "note": r.note,
        }
        for r in rows
    ]


@audit_router.get("/export")
def export_audit(
    request: Request,
    start: date | None = None,
    end: date | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("view_audit")
    import csv
    import io

    query = select(AuditRecord).where(AuditRecord.org_id == principal.org_id)
    if start:
        query = query.where(AuditRecord.occurred_at >= datetime.combine(start, datetime.min.time()))
    if end:
        query = query.where(AuditRecord.occurred_at <= datetime.combine(end, datetime.max.time()))
    rows = db.scalars(query.order_by(AuditRecord.occurred_at)).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        ["occurred_at", "actor_user_id", "actor_role", "action", "entity_type",
         "entity_id", "ip", "note", "before", "after"]
    )
    for row in rows:
        writer.writerow(
            [row.occurred_at.isoformat(), row.actor_user_id or "", row.actor_role or "",
             row.action, row.entity_type, row.entity_id or "", row.ip or "", row.note,
             row.before_json or "", row.after_json or ""]
        )
    audit_service.record_for(
        db, principal, request, action="audit.exported", entity_type="audit_record",
        after={"rows": len(rows)},
    )
    db.commit()
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit_log.csv"'},
    )


# ---------------------------------------------------------------------------
# Transparency notice (DP-05)
# ---------------------------------------------------------------------------


@privacy_router.get("/notice")
def privacy_notice(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    """DP-05: in-product, plain-language explanation of what is recorded, why,
    for how long, and who can see it."""
    org = db.get(Organisation, principal.org_id)
    return {
        "what_is_recorded": [
            "When you clock in and out, and the channel you used (web, kiosk, mobile).",
            "Breaks you start and end, and whether each is paid or unpaid.",
            "Absence you request, its type and its dates.",
            "The device identifier and IP address of the clock-in, for security.",
            "Where a site geofence is switched on: only whether you were inside it "
            "at the moment of clocking in — never a location track.",
        ],
        "what_is_not_recorded": [
            "No screenshots.",
            "No monitoring of applications, websites or keystrokes.",
            "No continuous location tracking.",
            "No biometric identification.",
        ],
        "why": {
            "legal_obligation": "Statutory working-time records (Art. 6(1)(c)).",
            "contract": "Calculating your pay (Art. 6(1)(b)).",
            "legitimate_interests": (
                "Operational scheduling and staffing (Art. 6(1)(f)), supported by a "
                "documented balancing test."
            ),
        },
        "who_can_see_it": [
            "You — all of your own data, at any time.",
            "Your line manager — your team's attendance, for approval and scheduling.",
            "HR and payroll — as far as their function requires.",
            "Every access to another employee's detail is written to the audit log.",
        ],
        "how_long": (
            f"Attendance records are kept for {org.retention_years} years after the end "
            "of the calendar year, then irreversibly anonymised or deleted."
        ),
        "your_rights": {
            "access_and_portability": "Download everything held about you (Art. 15, 20).",
            "rectification": "Raise a correction request on any record (Art. 16).",
            "no_automated_decisions": (
                "The system may flag an exception, but a person always decides "
                "(Art. 22). Attendance data is not used for performance profiling."
            ),
        },
        "data_location": "All personal data is stored and processed within the EU/EEA.",
        "contact": "Your Data Protection Officer.",
    }
