"""Absence policies, balances and the request lifecycle (Module F).

BR-08: an absence request consumes balance at the point of approval, not at
the point of request; planned absence is shown separately from taken.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AbsenceBalance,
    AbsencePolicy,
    AbsenceRequest,
    Organisation,
    Team,
    TimeBankMovement,
    User,
    new_id,
)
from . import calc, timeutil as T


# ---------------------------------------------------------------------------
# Entitlement arithmetic
# ---------------------------------------------------------------------------


def average_day_minutes(db: Session, user: User, on: date | None = None) -> int:
    """One 'day' of leave in minutes, derived from the working pattern so that
    part-time employees are handled correctly."""
    pattern = calc.pattern_for(db, user.id, on or date.today())
    if pattern is None:
        return 480
    values = [int(v or 0) for v in (pattern.expected_minutes or [])]
    working = [v for v in values if v > 0]
    if not working:
        return 480
    return int(round(sum(working) / len(working)))


def requested_minutes(
    db: Session, user: User, start: date, end: date, part_day_hours: float | None
) -> int:
    """FR-F-03 / US-05 AC-2: weekends and public holidays are excluded because
    they carry no expected hours."""
    if part_day_hours and start == end:
        expected, _ = calc.expected_minutes(db, user, start)
        return min(expected, int(round(part_day_hours * 60)))
    total = 0
    for day in T.daterange(start, end):
        expected, _ = calc.expected_minutes(db, user, day)
        total += expected
    return total


def entitlement_minutes(db: Session, user: User, policy: AbsencePolicy, year: int) -> int:
    if policy.accrual_method == "unlimited":
        return 0
    day_minutes = average_day_minutes(db, user, date(year, 6, 30))
    return int(round(policy.accrual_rate_days * day_minutes))


def accrued_minutes(
    db: Session, user: User, policy: AbsencePolicy, year: int, on: date | None = None
) -> int:
    on = on or date.today()
    full = entitlement_minutes(db, user, policy, year)
    if policy.accrual_method == "monthly":
        if on.year > year:
            months = 12
        elif on.year < year:
            months = 0
        else:
            months = on.month
        start_month = 1
        if user.employment_start and user.employment_start.year == year:
            start_month = user.employment_start.month
        months = max(0, min(12, months - start_month + 1))
        return int(round(full * months / 12))
    if policy.accrual_method == "unlimited":
        return 0
    return full


def balance_for(
    db: Session, user: User, policy: AbsencePolicy, year: int, today: date | None = None
) -> dict:
    today = today or date.today()
    record = db.scalar(
        select(AbsenceBalance).where(
            AbsenceBalance.user_id == user.id,
            AbsenceBalance.policy_id == policy.id,
            AbsenceBalance.year == year,
        )
    )
    carried = record.carried_over_minutes if record else 0
    adjustment = record.adjustment_minutes if record else 0

    approved = db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.user_id == user.id,
            AbsenceRequest.policy_id == policy.id,
            AbsenceRequest.status == "approved",
            AbsenceRequest.start_date >= date(year, 1, 1),
            AbsenceRequest.start_date <= date(year, 12, 31),
        )
    ).all()
    taken = sum(r.deducted_minutes for r in approved if r.end_date < today)
    planned = sum(r.deducted_minutes for r in approved if r.end_date >= today)

    pending = sum(
        r.deducted_minutes
        for r in db.scalars(
            select(AbsenceRequest).where(
                AbsenceRequest.user_id == user.id,
                AbsenceRequest.policy_id == policy.id,
                AbsenceRequest.status == "pending",
                AbsenceRequest.start_date >= date(year, 1, 1),
                AbsenceRequest.start_date <= date(year, 12, 31),
            )
        ).all()
    )

    entitlement = entitlement_minutes(db, user, policy, year)
    accrued = accrued_minutes(db, user, policy, year, today)
    day_minutes = average_day_minutes(db, user)
    remaining = accrued + carried + adjustment - taken - planned
    return {
        "policy_id": policy.id,
        "policy_name": policy.name,
        "is_paid": policy.is_paid,
        "unlimited": policy.accrual_method == "unlimited",
        "year": year,
        "day_minutes": day_minutes,
        "entitlement_minutes": entitlement,
        "accrued_minutes": accrued,
        "carried_over_minutes": carried,
        "adjustment_minutes": adjustment,
        "taken_minutes": taken,
        "planned_minutes": planned,
        "pending_minutes": pending,
        "remaining_minutes": remaining,
    }


def all_balances(db: Session, user: User, year: int | None = None) -> list[dict]:
    year = year or date.today().year
    policies = db.scalars(
        select(AbsencePolicy).where(
            AbsencePolicy.org_id == user.org_id, AbsencePolicy.archived.is_(False)
        )
    ).all()
    return [balance_for(db, user, policy, year) for policy in policies]


# ---------------------------------------------------------------------------
# Validation (FR-F-03, FR-F-04)
# ---------------------------------------------------------------------------


def team_coverage_warning(
    db: Session, user: User, policy: AbsencePolicy, start: date, end: date
) -> str | None:
    if not policy.min_team_coverage or not user.team_id:
        return None
    team = db.get(Team, user.team_id)
    members = db.scalars(
        select(User).where(
            User.org_id == user.org_id,
            User.team_id == user.team_id,
            User.status == "active",
        )
    ).all()
    for day in T.daterange(start, end):
        expected, _ = calc.expected_minutes(db, user, day)
        if expected <= 0:
            continue
        absent = 0
        for member in members:
            if member.id == user.id:
                absent += 1
                continue
            overlapping = db.scalar(
                select(AbsenceRequest).where(
                    AbsenceRequest.user_id == member.id,
                    AbsenceRequest.status == "approved",
                    AbsenceRequest.start_date <= day,
                    AbsenceRequest.end_date >= day,
                )
            )
            if overlapping:
                absent += 1
        present = len(members) - absent
        if present < policy.min_team_coverage:
            name = team.name if team else "the team"
            return (
                f"Approving this would leave {present} of {len(members)} in {name} "
                f"present on {day.isoformat()}; the minimum coverage is "
                f"{policy.min_team_coverage}."
            )
    return None


def validate(
    db: Session,
    user: User,
    policy: AbsencePolicy,
    start: date,
    end: date,
    part_day_hours: float | None,
    today: date | None = None,
    exclude_request_id: str | None = None,
) -> tuple[int, list[str], list[str]]:
    """Returns (deducted minutes, blocking errors, warnings)."""
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []

    if end < start:
        errors.append("The end date is before the start date.")
        return 0, errors, warnings

    minutes = requested_minutes(db, user, start, end, part_day_hours)
    if minutes <= 0:
        errors.append(
            "The selected range contains no working days (weekends and public "
            "holidays are excluded)."
        )

    # Overlap with existing requests
    query = select(AbsenceRequest).where(
        AbsenceRequest.user_id == user.id,
        AbsenceRequest.status.in_(("pending", "approved")),
        AbsenceRequest.start_date <= end,
        AbsenceRequest.end_date >= start,
    )
    if exclude_request_id:
        query = query.where(AbsenceRequest.id != exclude_request_id)
    if db.scalar(query):
        errors.append("This range overlaps an existing absence request.")

    # Notice period
    if policy.notice_days and start < today + timedelta(days=policy.notice_days):
        message = (
            f"The policy requires {policy.notice_days} days' notice; "
            f"the earliest possible start is "
            f"{(today + timedelta(days=policy.notice_days)).isoformat()}."
        )
        if start < today:
            warnings.append("Retrospective request — " + message)
        else:
            errors.append(message)

    # Balance (US-05 AC-3)
    if policy.accrual_method != "unlimited":
        balance = balance_for(db, user, policy, start.year, today)
        available = balance["remaining_minutes"] - balance["pending_minutes"]
        if minutes > available and not policy.allow_negative:
            errors.append(
                f"Insufficient balance: {minutes / 60:.1f} h requested, "
                f"{available / 60:.1f} h available and the policy does not allow "
                "a negative balance."
            )
        elif minutes > available:
            warnings.append(
                f"This request takes the balance negative by "
                f"{(minutes - available) / 60:.1f} h."
            )

    if policy.requires_document:
        warnings.append("This policy requires a supporting document before approval.")

    coverage = team_coverage_warning(db, user, policy, start, end)
    if coverage:
        warnings.append(coverage)

    return minutes, errors, warnings


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def approvers_for(db: Session, user: User, policy: AbsencePolicy, stage: int) -> list[str]:
    """Resolve the approver chain stage to concrete user ids."""
    chain = list(policy.approver_chain or ["manager"])
    if stage >= len(chain):
        return []
    role = chain[stage]
    if role == "manager":
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
        if ids:
            return ids
        role = "hr"
    return list(
        db.scalars(
            select(User.id).where(
                User.org_id == user.org_id,
                User.role == ("hr" if role == "hr" else role),
                User.status == "active",
            )
        ).all()
    )


def may_decide(db: Session, principal, request: AbsenceRequest) -> bool:
    if principal.id == request.user_id:
        return False
    scope = principal.scope("approve_absence")
    if scope == "all":
        return True
    if scope == "team":
        from ..security import visible_user_ids

        allowed = visible_user_ids(db, principal, "approve_absence")
        return allowed is None or request.user_id in allowed
    return False


def advance(
    db: Session, request: AbsenceRequest, policy: AbsencePolicy, decider_id: str
) -> str:
    """Move to the next stage of the approver chain, or approve outright."""
    chain = list(policy.approver_chain or ["manager"])
    if request.stage + 1 < len(chain):
        request.stage += 1
        return "pending"
    request.status = "approved"
    request.decided_by = decider_id
    return "approved"


def on_approved(
    db: Session, org: Organisation, user: User, request: AbsenceRequest,
    policy: AbsencePolicy,
) -> None:
    """FR-F-05: approved absence suppresses missing-attendance exceptions, so
    the affected days are recomputed and re-evaluated."""
    if policy.funded_from_time_bank and request.deducted_minutes:
        db.add(
            TimeBankMovement(
                id=new_id(),
                user_id=user.id,
                occurred_on=request.start_date,
                minutes=-request.deducted_minutes,
                kind="time_off_in_lieu",
                ref_id=request.id,
                note=f"Compensatory time off {request.start_date}–{request.end_date}",
            )
        )
    from . import rules

    rules.refresh(db, org, user, request.start_date, request.end_date)


def time_bank_balance(db: Session, user_id: str) -> int:
    rows = db.scalars(
        select(TimeBankMovement.minutes).where(TimeBankMovement.user_id == user_id)
    ).all()
    return int(sum(rows))
