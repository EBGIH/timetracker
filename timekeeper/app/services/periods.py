"""Attendance periods, submission state and locking (Module H)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Approval, Organisation, Period, User, new_id
from . import timeutil as T

OPEN, SUBMITTED, APPROVED, REJECTED, LOCKED = (
    "open", "submitted", "approved", "rejected", "locked",
)


def bounds_for(org: Organisation, day: date) -> tuple[date, date]:
    return T.period_bounds(org.period_type, day, org.week_start, org.period_anchor)


def ensure_period(db: Session, org: Organisation, day: date) -> Period:
    start, end = bounds_for(org, day)
    period = db.scalar(
        select(Period).where(
            Period.org_id == org.id, Period.start_date == start, Period.end_date == end
        )
    )
    if period is None:
        period = Period(
            id=new_id(),
            org_id=org.id,
            start_date=start,
            end_date=end,
            status=OPEN,
            cutoff_date=end + timedelta(days=org.submission_cutoff_days or 0),
        )
        db.add(period)
        db.flush()
    return period


def ensure_approval(db: Session, period: Period, user_id: str) -> Approval:
    approval = db.scalar(
        select(Approval).where(
            Approval.period_id == period.id, Approval.user_id == user_id
        )
    )
    if approval is None:
        approval = Approval(
            id=new_id(), period_id=period.id, user_id=user_id, status=OPEN
        )
        db.add(approval)
        db.flush()
    return approval


def state_for(db: Session, org: Organisation, user_id: str, day: date) -> str:
    """The editing state of one employee's records on one day."""
    period = ensure_period(db, org, day)
    if period.status == LOCKED:
        return LOCKED
    approval = ensure_approval(db, period, user_id)
    if approval.status in (SUBMITTED, APPROVED):
        return approval.status
    return OPEN


def assert_editable(
    db: Session,
    org: Organisation,
    user_id: str,
    day: date,
    principal,
    action: str = "edit",
) -> None:
    """Records in a locked period are immutable (section 12.3); a submitted or
    approved period is read-only to the employee but amendable by an approver,
    which always creates an audited revision (section 9.1 footnote *)."""
    state = state_for(db, org, user_id, day)
    if state == LOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "period_locked",
                "message": (
                    "This period is locked. Raise a correction request; it will be "
                    "routed for approval and will create a new record version."
                ),
                "day": day.isoformat(),
            },
        )
    if state in (SUBMITTED, APPROVED):
        is_self = principal.id == user_id
        may_amend = principal.can("edit_other_entry") and not is_self
        if not may_amend:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": f"period_{state}",
                    "message": (
                        f"This period is {state} and read-only for you. "
                        "Raise a correction request instead."
                    ),
                    "day": day.isoformat(),
                },
            )


def period_users(db: Session, org: Organisation) -> list[User]:
    return list(
        db.scalars(
            select(User).where(User.org_id == org.id, User.status == "active")
        ).all()
    )


def periods_between(db: Session, org: Organisation, start: date, end: date) -> list[Period]:
    """Materialise every period touching a range so reports can name them."""
    out: list[Period] = []
    cursor = start
    while cursor <= end:
        period = ensure_period(db, org, cursor)
        out.append(period)
        cursor = period.end_date + timedelta(days=1)
    return out
