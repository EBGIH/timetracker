"""Submission, approval, locking, corrections and overtime approval
(Modules G and H)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AttendanceException,
    AttendanceSession,
    CorrectionRequest,
    Organisation,
    OvertimeApproval,
    Period,
    TimeBankMovement,
    TimeEntry,
    User,
    new_id,
    utcnow,
)
from ..schemas import (
    CorrectionDecision,
    CorrectionIn,
    DecisionRequest,
    ExclusionRequest,
    LockRequest,
    OvertimeRequest,
    SubmitRequest,
)
from ..security import Principal, assert_may_view, get_principal, visible_user_ids
from ..services import calc, notifications, periods, rules, webhooks
from .attendance import local_day

router = APIRouter(prefix="/api", tags=["approvals"])


def org_of(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(404, "Organisation not found")
    return org


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


@router.get("/periods")
def list_periods(
    on: date | None = None,
    count: int = 6,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    cursor = on or date.today()
    out = []
    for _ in range(max(1, min(count, 36))):
        period = periods.ensure_period(db, org, cursor)
        out.append(period)
        cursor = period.start_date - timedelta(days=1)
    db.commit()
    return [
        {
            "id": p.id, "start_date": p.start_date, "end_date": p.end_date,
            "status": p.status, "cutoff_date": p.cutoff_date, "locked_at": p.locked_at,
        }
        for p in out
    ]


# ---------------------------------------------------------------------------
# Submission (FR-H-01, FR-H-04)
# ---------------------------------------------------------------------------


@router.post("/approvals/submit")
def submit_period(
    payload: SubmitRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    period = (
        db.get(Period, payload.period_id)
        if payload.period_id
        else periods.ensure_period(db, org, payload.day or date.today())
    )
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")
    if period.status == periods.LOCKED:
        raise HTTPException(409, "This period is locked")

    # Re-evaluate the whole period first, and persist that evaluation before
    # deciding: if submission is refused, the employee must still be able to
    # see and resolve the exceptions that refused it.
    rules.refresh(db, org, principal.user, period.start_date, period.end_date)
    db.commit()
    blocking = rules.blocking_exceptions(
        db, principal.id, period.start_date, period.end_date
    )
    if blocking:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "blocking_exceptions",
                "message": "Resolve these before submitting the period.",
                "exceptions": [
                    {"id": e.id, "day": e.day.isoformat(), "type": e.type,
                     "detail": e.detail}
                    for e in blocking
                ],
            },
        )

    approval = periods.ensure_approval(db, period, principal.id)
    if approval.status in (periods.SUBMITTED, periods.APPROVED):
        raise HTTPException(409, f"The period is already {approval.status}")
    before = audit.snapshot(approval)
    approval.status = periods.SUBMITTED
    approval.submitted_at = utcnow()
    approval.reason = ""
    audit.record_for(
        db, principal, request, action="period.submitted", entity_type="approval",
        entity_id=approval.id, before=before, after=approval,
    )
    webhooks.emit(db, org.id, "period_submitted",
                  {"user_id": principal.id, "period_id": period.id})

    approver_ids = _approvers_for_user(db, principal.user)
    notifications.notify_many(
        db, approver_ids, "timesheet_awaiting", "A timesheet awaits your approval",
        f"{principal.user.display_name} submitted "
        f"{period.start_date.isoformat()} – {period.end_date.isoformat()}.",
        f"/#/approvals?period={period.id}",
    )
    db.commit()
    return {"status": approval.status, "period_id": period.id}


def _approvers_for_user(db: Session, user: User) -> list[str]:
    from ..models import Team

    ids: list[str] = []
    team_id = user.team_id
    seen = set()
    while team_id and team_id not in seen:
        seen.add(team_id)
        team = db.get(Team, team_id)
        if team is None:
            break
        if team.manager_user_id and team.manager_user_id != user.id:
            ids.append(team.manager_user_id)
            break
        team_id = team.parent_team_id
    if not ids:
        ids = list(
            db.scalars(
                select(User.id).where(
                    User.org_id == user.org_id,
                    User.role.in_(("hr", "admin")),
                    User.status == "active",
                )
            ).all()
        )
    return ids


# ---------------------------------------------------------------------------
# Approval queue (US-04)
# ---------------------------------------------------------------------------


@router.get("/approvals/queue")
def approval_queue(
    period_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("approve_timesheet")
    org = org_of(principal, db)
    period = db.get(Period, period_id)
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")

    allowed = visible_user_ids(db, principal, "approve_timesheet")
    query = select(User).where(User.org_id == org.id, User.status == "active")
    if allowed is not None:
        query = query.where(User.id.in_(allowed))
    users = db.scalars(query.order_by(User.last_name)).all()

    rows = []
    for user in users:
        if user.id == principal.id and principal.scope("approve_timesheet") == "team":
            continue
        approval = periods.ensure_approval(db, period, user.id)
        totals = calc.period_totals(db, user.id, period.start_date, period.end_date)
        blocking = rules.blocking_exceptions(db, user.id, period.start_date, period.end_date)
        open_count = len(
            db.scalars(
                select(AttendanceException.id).where(
                    AttendanceException.user_id == user.id,
                    AttendanceException.day >= period.start_date,
                    AttendanceException.day <= period.end_date,
                    AttendanceException.status == "open",
                )
            ).all()
        )
        rows.append(
            {
                "user_id": user.id,
                "name": user.display_name,
                "personnel_number": user.personnel_number,
                "status": approval.status,
                "excluded": approval.excluded,
                "submitted_at": approval.submitted_at,
                "worked_minutes": totals["net_worked_minutes"],
                "expected_minutes": totals["expected_minutes"],
                "absence_minutes": totals["absence_minutes"],
                "difference_minutes": totals["balance_minutes"],
                "overtime_minutes": totals["overtime_total"],
                "exception_count": open_count,
                "blocking_count": len(blocking),
                "can_approve": not blocking and approval.status == periods.SUBMITTED,
            }
        )
    db.commit()
    return {
        "period": {
            "id": period.id, "start_date": period.start_date, "end_date": period.end_date,
            "status": period.status, "cutoff_date": period.cutoff_date,
        },
        "rows": rows,
    }


@router.post("/approvals/approve")
def approve(
    payload: DecisionRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-H-03: bulk approval from the queue view."""
    principal.require("approve_timesheet")
    org = org_of(principal, db)
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")
    if period.status == periods.LOCKED:
        raise HTTPException(409, "This period is locked")

    approved, skipped = [], []
    for user_id in payload.user_ids:
        if user_id == principal.id and principal.scope("approve_timesheet") == "team":
            skipped.append({"user_id": user_id, "reason": "cannot approve your own period"})
            continue
        assert_may_view(db, principal, user_id)
        blocking = rules.blocking_exceptions(db, user_id, period.start_date, period.end_date)
        if blocking:
            # US-04 AC-2
            skipped.append(
                {"user_id": user_id, "reason": "unresolved blocking exceptions",
                 "count": len(blocking)}
            )
            continue
        approval = periods.ensure_approval(db, period, user_id)
        before = audit.snapshot(approval)
        approval.status = periods.APPROVED
        approval.decided_by = principal.id
        approval.decided_at = utcnow()
        approval.reason = payload.reason
        audit.record_for(
            db, principal, request, action="period.approved", entity_type="approval",
            entity_id=approval.id, before=before, after=approval,
        )
        _bank_period_surplus(db, org, user_id, period)
        notifications.notify(
            db, user_id, "period_decided", "Your timesheet was approved",
            f"{period.start_date.isoformat()} – {period.end_date.isoformat()}",
            f"/#/tracker?date={period.end_date.isoformat()}",
        )
        webhooks.emit(db, org.id, "period_approved", {"user_id": user_id, "period_id": period.id})
        approved.append(user_id)
    db.commit()
    return {"approved": approved, "skipped": skipped}


def _bank_period_surplus(db: Session, org: Organisation, user_id: str, period: Period) -> None:
    """FR-G-05: approved surplus feeds the time bank, capped per the rule."""
    rule = calc.get_overtime_rule(db, org.id)
    if not rule.time_bank_enabled:
        return
    existing = db.scalar(
        select(TimeBankMovement).where(
            TimeBankMovement.user_id == user_id,
            TimeBankMovement.ref_id == period.id,
        )
    )
    if existing:
        return
    totals = calc.period_totals(db, user_id, period.start_date, period.end_date)
    surplus = totals["overtime_approved_minutes"]
    if surplus <= 0:
        return
    current = sum(
        db.scalars(
            select(TimeBankMovement.minutes).where(TimeBankMovement.user_id == user_id)
        ).all()
    )
    room = max(0, (rule.time_bank_cap_minutes or 0) - current)
    credited = min(surplus, room) if rule.time_bank_cap_minutes else surplus
    if credited <= 0:
        return
    db.add(
        TimeBankMovement(
            id=new_id(), user_id=user_id, occurred_on=period.end_date,
            minutes=credited, kind="accrual", ref_id=period.id,
            note=f"Approved overtime {period.start_date}–{period.end_date}",
        )
    )


@router.post("/approvals/reject")
def reject(
    payload: DecisionRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """US-04 AC-4: a reason is mandatory and the period returns to editable."""
    principal.require("approve_timesheet")
    if not payload.reason or len(payload.reason.strip()) < 3:
        raise HTTPException(400, "A reason is required when rejecting a period")
    org = org_of(principal, db)
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")
    for user_id in payload.user_ids:
        assert_may_view(db, principal, user_id)
        approval = periods.ensure_approval(db, period, user_id)
        before = audit.snapshot(approval)
        approval.status = periods.OPEN
        approval.decided_by = principal.id
        approval.decided_at = utcnow()
        approval.reason = payload.reason
        approval.submitted_at = None
        audit.record_for(
            db, principal, request, action="period.rejected", entity_type="approval",
            entity_id=approval.id, before=before, after=approval, note=payload.reason,
        )
        notifications.notify(
            db, user_id, "period_decided", "Your timesheet was returned",
            payload.reason, f"/#/tracker?date={period.end_date.isoformat()}",
        )
    db.commit()
    return {"status": "rejected", "reason": payload.reason}


@router.post("/approvals/exclude")
def exclude_from_period(
    payload: ExclusionRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """BR-09: locking requires every employee to be approved or explicitly
    excluded with a reason."""
    principal.require("lock_period")
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != principal.org_id:
        raise HTTPException(404, "Period not found")
    approval = periods.ensure_approval(db, period, payload.user_id)
    before = audit.snapshot(approval)
    approval.excluded = True
    approval.exclusion_reason = payload.reason
    audit.record_for(
        db, principal, request, action="period.excluded", entity_type="approval",
        entity_id=approval.id, before=before, after=approval, note=payload.reason,
    )
    db.commit()
    return {"status": "excluded"}


# ---------------------------------------------------------------------------
# Locking (FR-H-05, BR-09)
# ---------------------------------------------------------------------------


@router.post("/approvals/lock")
def lock_period(
    payload: LockRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("lock_period")
    org = org_of(principal, db)
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")
    if period.status == periods.LOCKED:
        raise HTTPException(409, "Already locked")

    outstanding = []
    for user in db.scalars(
        select(User).where(User.org_id == org.id, User.status == "active")
    ).all():
        approval = periods.ensure_approval(db, period, user.id)
        if approval.excluded or approval.status == periods.APPROVED:
            continue
        outstanding.append({"user_id": user.id, "name": user.display_name,
                            "status": approval.status})
    if outstanding:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "not_all_approved",
                "message": (
                    "A period can be locked only when every employee in scope is "
                    "either approved or explicitly excluded with a reason (BR-09)."
                ),
                "outstanding": outstanding,
            },
        )
    before = audit.snapshot(period)
    period.status = periods.LOCKED
    period.locked_at = utcnow()
    period.locked_by = principal.id
    audit.record_for(
        db, principal, request, action="period.locked", entity_type="period",
        entity_id=period.id, before=before, after=period, note=payload.reason,
    )
    webhooks.emit(db, org.id, "period_locked", {"period_id": period.id})
    db.commit()
    return {"status": "locked", "locked_at": period.locked_at}


@router.post("/approvals/unlock")
def unlock_period(
    payload: LockRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-H-05: no edit is possible without an explicit, audited unlock."""
    principal.require("lock_period")
    if not payload.reason or len(payload.reason.strip()) < 3:
        raise HTTPException(400, "A reason is required to unlock a period")
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != principal.org_id:
        raise HTTPException(404, "Period not found")
    before = audit.snapshot(period)
    period.status = periods.OPEN
    period.locked_at = None
    period.locked_by = None
    audit.record_for(
        db, principal, request, action="period.unlocked", entity_type="period",
        entity_id=period.id, before=before, after=period, note=payload.reason,
    )
    db.commit()
    return {"status": "open"}


# ---------------------------------------------------------------------------
# Corrections (FR-H-06, section 8.4)
# ---------------------------------------------------------------------------


@router.post("/corrections", status_code=201)
def raise_correction(
    payload: CorrectionIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    model = AttendanceSession if payload.entity_type == "attendance_session" else TimeEntry
    entity = db.get(model, payload.entity_id)
    if entity is None or entity.org_id != org.id:
        raise HTTPException(404, "Record not found")
    if entity.user_id != principal.id:
        principal.require("edit_other_entry")
        assert_may_view(db, principal, entity.user_id)

    day = (
        entity.day
        if payload.entity_type == "time_entry"
        else local_day(db, org, db.get(User, entity.user_id), entity.start_at)
    )
    correction = CorrectionRequest(
        id=new_id(), org_id=org.id, user_id=entity.user_id, raised_by=principal.id,
        entity_type=payload.entity_type, entity_id=payload.entity_id, day=day,
        proposed_json=payload.proposed, reason=payload.reason,
    )
    db.add(correction)
    audit.record_for(
        db, principal, request, action="correction.raised",
        entity_type="correction_request", entity_id=correction.id, after=correction,
    )
    notifications.notify_many(
        db, _approvers_for_user(db, db.get(User, entity.user_id)),
        "correction_awaiting", "A correction request awaits approval",
        f"For {day.isoformat()}.", "/#/approvals",
    )
    db.commit()
    return {"id": correction.id, "status": correction.status}


@router.get("/corrections")
def list_corrections(
    status_filter: str = "pending",
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(CorrectionRequest).where(CorrectionRequest.org_id == principal.org_id)
    if status_filter != "all":
        query = query.where(CorrectionRequest.status == status_filter)
    if not principal.can("edit_other_entry"):
        query = query.where(CorrectionRequest.user_id == principal.id)
    else:
        allowed = visible_user_ids(db, principal, "edit_other_entry")
        if allowed is not None:
            query = query.where(CorrectionRequest.user_id.in_(allowed))
    rows = db.scalars(query.order_by(CorrectionRequest.created_at.desc())).all()
    names = {u.id: u.display_name for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)
    ).all()}
    return [
        {
            "id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id, ""),
            "day": r.day, "entity_type": r.entity_type, "entity_id": r.entity_id,
            "proposed": r.proposed_json, "reason": r.reason, "status": r.status,
            "raised_by": names.get(r.raised_by, ""), "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/corrections/{correction_id}/approve")
def approve_correction(
    correction_id: str,
    payload: CorrectionDecision,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Section 8.4: the original record is never overwritten. A new version
    supersedes the previous one; both remain in the audit trail."""
    principal.require("edit_other_entry")
    org = org_of(principal, db)
    correction = db.get(CorrectionRequest, correction_id)
    if correction is None or correction.org_id != org.id:
        raise HTTPException(404, "Correction not found")
    if correction.status != "pending":
        raise HTTPException(409, f"Already {correction.status}")
    assert_may_view(db, principal, correction.user_id)

    user = db.get(User, correction.user_id)
    if correction.entity_type == "attendance_session":
        original = db.get(AttendanceSession, correction.entity_id)
        if original is None:
            raise HTTPException(404, "Original record no longer exists")
        before = audit.snapshot(original)
        replacement = AttendanceSession(
            id=new_id(), org_id=org.id, user_id=original.user_id,
            start_at=original.start_at, end_at=original.end_at,
            raw_start_at=original.raw_start_at, raw_end_at=original.raw_end_at,
            source=original.source, device_id=original.device_id, ip=original.ip,
            location_id=original.location_id, status=original.status,
            note=original.note, description=original.description,
            cost_centre_id=original.cost_centre_id, created_by=principal.id,
            version=original.version + 1,
        )
        for field, value in (correction.proposed_json or {}).items():
            if field in ("start_at", "end_at") and value:
                setattr(replacement, field, datetime.fromisoformat(value).replace(tzinfo=None))
            elif field in ("description", "note", "cost_centre_id"):
                setattr(replacement, field, value)
        if replacement.end_at and replacement.end_at <= replacement.start_at:
            raise HTTPException(400, "The corrected end time must be after the start")
        db.add(replacement)
        db.flush()
        original.superseded_by = replacement.id
        for brk in original.breaks:
            db.add(
                type(brk)(
                    id=new_id(), session_id=replacement.id,
                    break_type_id=brk.break_type_id, start_at=brk.start_at,
                    end_at=brk.end_at, is_paid=brk.is_paid, automatic=brk.automatic,
                )
            )
        audit.record_for(
            db, principal, request, action="correction.applied",
            entity_type="attendance_session", entity_id=replacement.id,
            before=before, after=replacement, note=correction.reason,
        )
    else:
        original = db.get(TimeEntry, correction.entity_id)
        if original is None:
            raise HTTPException(404, "Original record no longer exists")
        before = audit.snapshot(original)
        replacement = TimeEntry(
            id=new_id(), org_id=org.id, user_id=original.user_id, day=original.day,
            session_id=original.session_id, cost_centre_id=original.cost_centre_id,
            description=original.description,
            duration_minutes=int((correction.proposed_json or {}).get(
                "duration_minutes", original.duration_minutes)),
            source=original.source, version=original.version + 1,
            created_by=principal.id,
        )
        db.add(replacement)
        db.flush()
        original.superseded_by = replacement.id
        audit.record_for(
            db, principal, request, action="correction.applied",
            entity_type="time_entry", entity_id=replacement.id,
            before=before, after=replacement, note=correction.reason,
        )

    correction.status = "approved"
    correction.decided_by = principal.id
    correction.decided_at = utcnow()
    correction.decision_note = payload.note
    rules.refresh(db, org, user, correction.day, correction.day)
    notifications.notify(
        db, correction.user_id, "entry_amended", "Your correction was approved",
        f"{correction.day.isoformat()}", f"/#/tracker?date={correction.day.isoformat()}",
    )
    db.commit()
    return {"status": "approved", "new_record_id": replacement.id}


@router.post("/corrections/{correction_id}/reject")
def reject_correction(
    correction_id: str,
    payload: CorrectionDecision,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("edit_other_entry")
    correction = db.get(CorrectionRequest, correction_id)
    if correction is None or correction.org_id != principal.org_id:
        raise HTTPException(404, "Correction not found")
    if not payload.note:
        raise HTTPException(400, "A reason is required when rejecting a correction")
    correction.status = "rejected"
    correction.decided_by = principal.id
    correction.decided_at = utcnow()
    correction.decision_note = payload.note
    audit.record_for(
        db, principal, request, action="correction.rejected",
        entity_type="correction_request", entity_id=correction.id, note=payload.note,
    )
    notifications.notify(
        db, correction.user_id, "entry_amended", "Your correction was rejected",
        payload.note, "/#/tracker",
    )
    db.commit()
    return {"status": "rejected"}


# ---------------------------------------------------------------------------
# Overtime approval (FR-G-04)
# ---------------------------------------------------------------------------


@router.post("/overtime/requests", status_code=201)
def request_overtime(
    payload: OvertimeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user_id = payload.user_id or principal.id
    if user_id != principal.id:
        principal.require("edit_other_entry")
        assert_may_view(db, principal, user_id)
    existing = db.scalar(
        select(OvertimeApproval).where(
            OvertimeApproval.user_id == user_id, OvertimeApproval.day == payload.day
        )
    )
    if existing and existing.status == "approved":
        raise HTTPException(409, "Overtime for that day is already approved")
    record = existing or OvertimeApproval(
        id=new_id(), org_id=org.id, user_id=user_id, day=payload.day
    )
    record.minutes = payload.minutes
    record.reason = payload.reason
    record.status = "pending"
    db.add(record)
    audit.record_for(
        db, principal, request, action="overtime.requested",
        entity_type="overtime_approval", entity_id=record.id, after=record,
    )
    notifications.notify_many(
        db, _approvers_for_user(db, db.get(User, user_id)), "overtime_awaiting",
        "Overtime awaits your approval", f"{payload.day.isoformat()}", "/#/approvals",
    )
    db.commit()
    return {"id": record.id, "status": record.status}


@router.get("/overtime/requests")
def list_overtime(
    status_filter: str = "pending",
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(OvertimeApproval).where(OvertimeApproval.org_id == principal.org_id)
    if status_filter != "all":
        query = query.where(OvertimeApproval.status == status_filter)
    if not principal.can("approve_timesheet"):
        query = query.where(OvertimeApproval.user_id == principal.id)
    else:
        allowed = visible_user_ids(db, principal, "approve_timesheet")
        if allowed is not None:
            query = query.where(OvertimeApproval.user_id.in_(allowed))
    rows = db.scalars(query.order_by(OvertimeApproval.day.desc())).all()
    names = {u.id: u.display_name for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)).all()}
    return [
        {
            "id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id, ""),
            "day": r.day, "minutes": r.minutes, "status": r.status, "reason": r.reason,
        }
        for r in rows
    ]


@router.post("/overtime/requests/{request_id}/decide")
def decide_overtime(
    request_id: str,
    body: dict,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("approve_timesheet")
    org = org_of(principal, db)
    record = db.get(OvertimeApproval, request_id)
    if record is None or record.org_id != org.id:
        raise HTTPException(404, "Request not found")
    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")
    assert_may_view(db, principal, record.user_id)
    before = audit.snapshot(record)
    record.status = decision
    record.decided_by = principal.id
    record.decided_at = utcnow()
    audit.record_for(
        db, principal, request, action=f"overtime.{decision}",
        entity_type="overtime_approval", entity_id=record.id,
        before=before, after=record,
    )
    user = db.get(User, record.user_id)
    rules.refresh(db, org, user, record.day, record.day)
    notifications.notify(
        db, record.user_id, "period_decided", f"Overtime {decision}",
        record.day.isoformat(), f"/#/tracker?date={record.day.isoformat()}",
    )
    db.commit()
    return {"status": decision}


@router.get("/time-bank")
def time_bank(
    user_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    target = user_id or principal.id
    if target != principal.id:
        assert_may_view(db, principal, target)
    rows = db.scalars(
        select(TimeBankMovement)
        .where(TimeBankMovement.user_id == target)
        .order_by(TimeBankMovement.occurred_on.desc())
    ).all()
    return {
        "balance_minutes": sum(r.minutes for r in rows),
        "movements": [
            {"id": r.id, "occurred_on": r.occurred_on, "minutes": r.minutes,
             "kind": r.kind, "note": r.note}
            for r in rows
        ],
    }
