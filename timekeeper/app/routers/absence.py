"""Absence and time off (Module F)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AbsenceBalance,
    AbsencePolicy,
    AbsenceRequest,
    Organisation,
    User,
    new_id,
    utcnow,
)
from ..schemas import AbsenceDecision, AbsenceRequestIn, BalanceAdjustment
from ..security import Principal, assert_may_view, get_principal, visible_user_ids
from ..services import absence as service, notifications, timeutil as T, webhooks

router = APIRouter(prefix="/api/absence", tags=["absence"])


def org_of(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(404, "Organisation not found")
    return org


@router.get("/balances")
def balances(
    user_id: str | None = None,
    year: int | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-F-06: entitlement, accrued, taken, planned, remaining."""
    target = user_id or principal.id
    if target != principal.id:
        assert_may_view(db, principal, target)
    user = db.get(User, target)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    return service.all_balances(db, user, year)


@router.post("/preview")
def preview(
    payload: AbsenceRequestIn,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Live validation for the request form (US-05 AC-1 .. AC-3)."""
    target = payload.user_id or principal.id
    if target != principal.id:
        assert_may_view(db, principal, target)
    user = db.get(User, target)
    policy = db.get(AbsencePolicy, payload.policy_id)
    if user is None or policy is None or policy.org_id != principal.org_id:
        raise HTTPException(404, "User or policy not found")
    minutes, errors, warnings = service.validate(
        db, user, policy, payload.start_date, payload.end_date, payload.part_day_hours
    )
    return {
        "deducted_minutes": minutes,
        "deducted_days": round(minutes / max(1, service.average_day_minutes(db, user)), 2),
        "errors": errors,
        "warnings": warnings,
        "balance": service.balance_for(db, user, policy, payload.start_date.year),
    }


@router.post("/requests", status_code=201)
def create_request(
    payload: AbsenceRequestIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    target = payload.user_id or principal.id
    retrospective = False
    if target != principal.id:
        # FR-F-08: HR or a manager may record absence retrospectively.
        principal.require("approve_absence")
        assert_may_view(db, principal, target)
        retrospective = True
    user = db.get(User, target)
    policy = db.get(AbsencePolicy, payload.policy_id)
    if user is None or policy is None or policy.org_id != org.id:
        raise HTTPException(404, "User or policy not found")

    minutes, errors, warnings = service.validate(
        db, user, policy, payload.start_date, payload.end_date, payload.part_day_hours
    )
    if errors:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_failed", "errors": errors, "warnings": warnings},
        )
    if policy.requires_document and not payload.document_ref and not retrospective:
        raise HTTPException(400, "This policy requires a supporting document reference")

    absence_request = AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=user.id, policy_id=policy.id,
        start_date=payload.start_date, end_date=payload.end_date,
        part_day_hours=payload.part_day_hours, reason=payload.reason,
        document_ref=payload.document_ref, deducted_minutes=minutes,
        created_by=principal.id, retrospective=retrospective,
    )
    db.add(absence_request)
    db.flush()
    audit.record_for(
        db, principal, request, action="absence.requested",
        entity_type="absence_request", entity_id=absence_request.id, after=absence_request,
    )

    if retrospective and principal.can("approve_absence"):
        # Recorded by HR/manager on the employee's behalf — approved on entry.
        absence_request.status = "approved"
        absence_request.decided_by = principal.id
        absence_request.decided_at = utcnow()
        service.on_approved(db, org, user, absence_request, policy)
        notifications.notify(
            db, user.id, "absence_decided", "Absence recorded for you",
            f"{policy.name}: {payload.start_date} – {payload.end_date}", "/#/absence",
        )
    else:
        approvers = service.approvers_for(db, user, policy, 0)
        notifications.notify_many(
            db, approvers, "absence_awaiting", "An absence request awaits your approval",
            f"{user.display_name}: {policy.name}, {payload.start_date} – {payload.end_date}",
            "/#/absence?tab=approvals",
        )
    db.commit()
    return {
        "id": absence_request.id,
        "status": absence_request.status,
        "deducted_minutes": minutes,
        "warnings": warnings,
    }


@router.get("/requests")
def list_requests(
    scope: str = "self",
    status_filter: str = "all",
    start: date | None = None,
    end: date | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(AbsenceRequest).where(AbsenceRequest.org_id == principal.org_id)
    if scope == "self":
        query = query.where(AbsenceRequest.user_id == principal.id)
    else:
        allowed = None
        if principal.can("view_all_attendance"):
            allowed = None
        elif principal.can("view_team_attendance"):
            allowed = visible_user_ids(db, principal, "view_team_attendance")
        else:
            allowed = {principal.id}
        if allowed is not None:
            query = query.where(AbsenceRequest.user_id.in_(allowed))
    if status_filter != "all":
        query = query.where(AbsenceRequest.status == status_filter)
    if start:
        query = query.where(AbsenceRequest.end_date >= start)
    if end:
        query = query.where(AbsenceRequest.start_date <= end)

    rows = db.scalars(query.order_by(AbsenceRequest.start_date.desc())).all()
    names = {u.id: u.display_name for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)).all()}
    policies = {p.id: p for p in db.scalars(
        select(AbsencePolicy).where(AbsencePolicy.org_id == principal.org_id)).all()}
    return [
        {
            "id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id, ""),
            "policy_id": r.policy_id,
            "policy_name": policies[r.policy_id].name if r.policy_id in policies else "",
            "is_paid": policies[r.policy_id].is_paid if r.policy_id in policies else None,
            "start_date": r.start_date, "end_date": r.end_date,
            "part_day_hours": r.part_day_hours, "status": r.status, "stage": r.stage,
            "deducted_minutes": r.deducted_minutes, "reason": r.reason,
            "decision_note": r.decision_note, "requested_at": r.requested_at,
            "document_ref": r.document_ref,
        }
        for r in rows
    ]


@router.post("/requests/{request_id}/approve")
def approve_request(
    request_id: str,
    payload: AbsenceDecision,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    absence_request = db.get(AbsenceRequest, request_id)
    if absence_request is None or absence_request.org_id != org.id:
        raise HTTPException(404, "Request not found")
    if absence_request.status != "pending":
        raise HTTPException(409, f"Already {absence_request.status}")
    if not service.may_decide(db, principal, absence_request):
        raise HTTPException(403, "You may not decide this request")

    policy = db.get(AbsencePolicy, absence_request.policy_id)
    user = db.get(User, absence_request.user_id)

    # Re-validate at the moment of decision: the balance is consumed here (BR-08).
    minutes, errors, warnings = service.validate(
        db, user, policy, absence_request.start_date, absence_request.end_date,
        absence_request.part_day_hours, exclude_request_id=absence_request.id,
    )
    if errors:
        raise HTTPException(
            400, detail={"error": "no_longer_valid", "errors": errors}
        )
    absence_request.deducted_minutes = minutes

    before = audit.snapshot(absence_request)
    outcome = service.advance(db, absence_request, policy, principal.id)
    absence_request.decided_at = utcnow()
    absence_request.decision_note = payload.note
    audit.record_for(
        db, principal, request, action=f"absence.{outcome}",
        entity_type="absence_request", entity_id=absence_request.id,
        before=before, after=absence_request,
    )
    if outcome == "approved":
        service.on_approved(db, org, user, absence_request, policy)
        webhooks.emit(db, org.id, "absence_approved",
                      {"user_id": user.id, "request_id": absence_request.id})
        notifications.notify(
            db, user.id, "absence_decided", "Your absence request was approved",
            f"{policy.name}: {absence_request.start_date} – {absence_request.end_date}",
            "/#/absence",
        )
    else:
        notifications.notify_many(
            db, service.approvers_for(db, user, policy, absence_request.stage),
            "absence_awaiting", "An absence request awaits your approval",
            f"{user.display_name}: {policy.name}", "/#/absence?tab=approvals",
        )
    db.commit()
    return {"status": absence_request.status, "stage": absence_request.stage,
            "warnings": warnings}


@router.post("/requests/{request_id}/reject")
def reject_request(
    request_id: str,
    payload: AbsenceDecision,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    absence_request = db.get(AbsenceRequest, request_id)
    if absence_request is None or absence_request.org_id != principal.org_id:
        raise HTTPException(404, "Request not found")
    if absence_request.status != "pending":
        raise HTTPException(409, f"Already {absence_request.status}")
    if not service.may_decide(db, principal, absence_request):
        raise HTTPException(403, "You may not decide this request")
    if not payload.note:
        raise HTTPException(400, "A reason is required when rejecting a request")
    before = audit.snapshot(absence_request)
    absence_request.status = "rejected"
    absence_request.decided_by = principal.id
    absence_request.decided_at = utcnow()
    absence_request.decision_note = payload.note
    audit.record_for(
        db, principal, request, action="absence.rejected",
        entity_type="absence_request", entity_id=absence_request.id,
        before=before, after=absence_request, note=payload.note,
    )
    notifications.notify(
        db, absence_request.user_id, "absence_decided",
        "Your absence request was rejected", payload.note, "/#/absence",
    )
    db.commit()
    return {"status": "rejected"}


@router.post("/requests/{request_id}/cancel")
def cancel_request(
    request_id: str,
    payload: AbsenceDecision,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """US-05 AC-5: cancelling before it starts restores the balance."""
    org = org_of(principal, db)
    absence_request = db.get(AbsenceRequest, request_id)
    if absence_request is None or absence_request.org_id != org.id:
        raise HTTPException(404, "Request not found")
    is_owner = absence_request.user_id == principal.id
    if not is_owner and not service.may_decide(db, principal, absence_request):
        raise HTTPException(403, "You may not cancel this request")
    if absence_request.status not in ("pending", "approved"):
        raise HTTPException(409, f"Cannot cancel a {absence_request.status} request")
    if absence_request.status == "approved" and absence_request.start_date <= date.today():
        if not principal.can("approve_absence"):
            raise HTTPException(
                409,
                "Absence that has already started can only be cancelled by HR or your manager.",
            )
    before = audit.snapshot(absence_request)
    absence_request.status = "cancelled"
    absence_request.decided_at = utcnow()
    absence_request.decision_note = payload.note
    audit.record_for(
        db, principal, request, action="absence.cancelled",
        entity_type="absence_request", entity_id=absence_request.id,
        before=before, after=absence_request,
    )
    user = db.get(User, absence_request.user_id)
    from ..services import rules as rules_service

    rules_service.refresh(db, org, user, absence_request.start_date, absence_request.end_date)
    notifications.notify(
        db, absence_request.user_id, "absence_decided", "An absence request was cancelled",
        f"{absence_request.start_date} – {absence_request.end_date}", "/#/absence",
    )
    db.commit()
    return {"status": "cancelled"}


@router.post("/balance-adjustment")
def adjust_balance(
    payload: BalanceAdjustment,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-F-07: a manual adjustment with a mandatory reason, recorded in the
    audit log."""
    principal.require("configure_policies")
    user = db.get(User, payload.user_id)
    policy = db.get(AbsencePolicy, payload.policy_id)
    if user is None or policy is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User or policy not found")
    record = db.scalar(
        select(AbsenceBalance).where(
            AbsenceBalance.user_id == payload.user_id,
            AbsenceBalance.policy_id == payload.policy_id,
            AbsenceBalance.year == payload.year,
        )
    )
    if record is None:
        record = AbsenceBalance(
            id=new_id(), user_id=payload.user_id, policy_id=payload.policy_id,
            year=payload.year,
        )
        db.add(record)
        db.flush()
    before = audit.snapshot(record)
    record.adjustment_minutes += payload.minutes
    record.adjustment_reason = payload.reason
    audit.record_for(
        db, principal, request, action="absence.balance_adjusted",
        entity_type="absence_balance", entity_id=record.id,
        before=before, after=record, note=payload.reason,
    )
    db.commit()
    return service.balance_for(db, user, policy, payload.year)


@router.post("/carry-over")
def run_carry_over(
    body: dict,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Roll unused balance into the next year, capped by the policy."""
    principal.require("configure_policies")
    from_year = int(body.get("from_year", date.today().year - 1))
    policies = db.scalars(
        select(AbsencePolicy).where(
            AbsencePolicy.org_id == principal.org_id,
            AbsencePolicy.archived.is_(False),
        )
    ).all()
    users = db.scalars(
        select(User).where(User.org_id == principal.org_id, User.status == "active")
    ).all()
    moved = 0
    for user in users:
        for policy in policies:
            if policy.carry_over_limit_days <= 0:
                continue
            balance = service.balance_for(
                db, user, policy, from_year, date(from_year, 12, 31)
            )
            surplus = max(0, balance["remaining_minutes"])
            cap = int(policy.carry_over_limit_days * balance["day_minutes"])
            carried = min(surplus, cap)
            if carried <= 0:
                continue
            record = db.scalar(
                select(AbsenceBalance).where(
                    AbsenceBalance.user_id == user.id,
                    AbsenceBalance.policy_id == policy.id,
                    AbsenceBalance.year == from_year + 1,
                )
            )
            if record is None:
                record = AbsenceBalance(
                    id=new_id(), user_id=user.id, policy_id=policy.id,
                    year=from_year + 1,
                )
                db.add(record)
            record.carried_over_minutes = carried
            moved += 1
    audit.record_for(
        db, principal, request, action="absence.carry_over_run",
        entity_type="absence_balance", after={"from_year": from_year, "records": moved},
    )
    db.commit()
    return {"from_year": from_year, "records_updated": moved}


@router.get("/calendar")
def team_calendar(
    start: date,
    end: date,
    team_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-F-05: approved absence appears in the team calendar."""
    allowed = None
    if principal.can("view_all_attendance"):
        allowed = None
    elif principal.can("view_team_attendance"):
        allowed = visible_user_ids(db, principal, "view_team_attendance")
    else:
        allowed = {principal.id}

    query = select(AbsenceRequest).where(
        AbsenceRequest.org_id == principal.org_id,
        AbsenceRequest.status.in_(("approved", "pending")),
        AbsenceRequest.start_date <= end,
        AbsenceRequest.end_date >= start,
    )
    if allowed is not None:
        query = query.where(AbsenceRequest.user_id.in_(allowed))
    rows = db.scalars(query).all()

    users = {u.id: u for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)).all()}
    policies = {p.id: p for p in db.scalars(
        select(AbsencePolicy).where(AbsencePolicy.org_id == principal.org_id)).all()}
    if team_id:
        rows = [r for r in rows if users.get(r.user_id) and users[r.user_id].team_id == team_id]

    entries = []
    for row in rows:
        user = users.get(row.user_id)
        policy = policies.get(row.policy_id)
        entries.append(
            {
                "user_id": row.user_id,
                "user_name": user.display_name if user else "",
                "policy": policy.name if policy else "",
                "is_paid": policy.is_paid if policy else None,
                "status": row.status,
                "days": [
                    d.isoformat()
                    for d in T.daterange(
                        max(row.start_date, start), min(row.end_date, end)
                    )
                ],
            }
        )
    return {"start": start, "end": end, "entries": entries}
