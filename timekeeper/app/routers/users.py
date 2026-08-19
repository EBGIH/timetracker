"""People management (Module B) and subject-access export (FR-L-05, DP-06)."""

from __future__ import annotations

import csv
import io
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AbsenceRequest,
    AttendanceException,
    AttendanceSession,
    Credential,
    DayAggregate,
    Invitation,
    Kiosk,
    Organisation,
    TimeEntry,
    User,
    WorkingPattern,
    new_id,
    utcnow,
)
from ..schemas import UserIn, UserUpdate, WorkingPatternIn
from ..security import (
    Principal,
    assert_may_view,
    get_principal,
    hash_secret,
    lookup_hash,
    random_pin,
    visible_user_ids,
)
from ..services import absence as absence_service, calc, rules

router = APIRouter(prefix="/api/users", tags=["people"])

ROLE_RANK = {"limited": 0, "employee": 1, "manager": 2, "hr": 3, "admin": 4, "owner": 5}


def _serialise(db: Session, user: User, include_pattern: bool = False) -> dict:
    data = {
        "id": user.id,
        "personnel_number": user.personnel_number,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "name": user.display_name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "team_id": user.team_id,
        "location_id": user.location_id,
        "employment_start": user.employment_start,
        "employment_end": user.employment_end,
        "has_login": user.has_login,
        "language": user.language,
        "mfa_enabled": user.mfa_enabled,
        "wt_optout_from": user.wt_optout_from,
    }
    if include_pattern:
        pattern = calc.pattern_for(db, user.id, date.today())
        data["working_pattern"] = (
            {
                "id": pattern.id,
                "valid_from": pattern.valid_from,
                "valid_to": pattern.valid_to,
                "contracted_hours_per_week": pattern.contracted_hours_per_week,
                "expected_minutes": pattern.expected_minutes,
                "shift_start": pattern.shift_start,
                "shift_end": pattern.shift_end,
            }
            if pattern
            else None
        )
    return data


@router.get("")
def list_users(
    include_inactive: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """DP-09 least privilege: a manager sees their own team, HR and above see
    the population their function requires."""
    query = select(User).where(User.org_id == principal.org_id)
    if not include_inactive:
        query = query.where(User.status == "active")

    allowed = None
    if principal.can("view_all_attendance"):
        allowed = None
    elif principal.can("view_team_attendance"):
        allowed = visible_user_ids(db, principal, "view_team_attendance")
    else:
        allowed = {principal.id}
    if allowed is not None:
        query = query.where(User.id.in_(allowed))

    rows = db.scalars(query.order_by(User.last_name, User.first_name)).all()
    return [_serialise(db, u, include_pattern=True) for u in rows]


@router.post("", status_code=201)
def create_user(
    payload: UserIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("manage_users")
    if ROLE_RANK[payload.role] > ROLE_RANK[principal.role]:
        raise HTTPException(403, "You cannot grant a role above your own")
    if payload.role == "owner":
        raise HTTPException(400, "Ownership is transferred, not granted (section 9)")
    if payload.has_login and not payload.email:
        raise HTTPException(400, "A login-capable user needs an e-mail address")
    if payload.role == "limited" and payload.has_login:
        raise HTTPException(
            400, "A limited member authenticates only at a kiosk (FR-B-02)"
        )
    existing = db.scalar(
        select(User).where(
            User.org_id == principal.org_id,
            User.personnel_number == payload.personnel_number,
        )
    )
    if existing:
        raise HTTPException(409, "That personnel number already exists")

    user = User(
        id=new_id(),
        org_id=principal.org_id,
        **payload.model_dump(),
    )
    if user.email:
        user.email = user.email.lower().strip()
    db.add(user)
    db.flush()
    # A default full-time pattern so capacity is defined from day one.
    db.add(
        WorkingPattern(
            id=new_id(),
            user_id=user.id,
            valid_from=user.employment_start,
            contracted_hours_per_week=40.0,
            expected_minutes=[480, 480, 480, 480, 480, 0, 0],
        )
    )
    audit.record_for(
        db, principal, request, action="user.created", entity_type="user",
        entity_id=user.id, after=user,
    )
    db.commit()
    return _serialise(db, user, include_pattern=True)


@router.get("/{user_id}")
def read_user(
    user_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    assert_may_view(db, principal, user_id)
    if user_id != principal.id:
        # DP-09: access to another employee's detail is logged.
        audit.record_for(
            db, principal, None, action="user.viewed", entity_type="user",
            entity_id=user_id,
        )
        db.commit()
    return _serialise(db, user, include_pattern=True)


@router.put("/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    data = payload.model_dump(exclude_unset=True)
    if "role" in data:
        if ROLE_RANK[data["role"]] > ROLE_RANK[principal.role]:
            raise HTTPException(403, "You cannot grant a role above your own")
        if user.role == "owner" and principal.role != "owner":
            raise HTTPException(403, "Only the owner may change the owner's role")
    before = audit.snapshot(user)
    for field, value in data.items():
        setattr(user, field, value.lower().strip() if field == "email" and value else value)
    audit.record_for(
        db, principal, request, action="user.updated", entity_type="user",
        entity_id=user.id, before=before, after=user,
    )
    db.commit()
    return _serialise(db, user, include_pattern=True)


@router.post("/{user_id}/deactivate")
def deactivate_user(
    user_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-B-06: historic records are preserved and remain reportable."""
    principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    if user.role == "owner":
        raise HTTPException(400, "Transfer ownership before deactivating the owner")
    before = audit.snapshot(user)
    user.status = "inactive"
    if not user.employment_end:
        user.employment_end = date.today()
    audit.record_for(
        db, principal, request, action="user.deactivated", entity_type="user",
        entity_id=user.id, before=before, after=user,
    )
    db.commit()
    return {"status": "inactive"}


# ---------------------------------------------------------------------------
# Working patterns (FR-B-04, FR-B-05, BR-11)
# ---------------------------------------------------------------------------


@router.get("/{user_id}/patterns")
def list_patterns(
    user_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    assert_may_view(db, principal, user_id)
    rows = db.scalars(
        select(WorkingPattern)
        .where(WorkingPattern.user_id == user_id)
        .order_by(WorkingPattern.valid_from)
    ).all()
    return [
        {
            "id": r.id, "valid_from": r.valid_from, "valid_to": r.valid_to,
            "contracted_hours_per_week": r.contracted_hours_per_week,
            "expected_minutes": r.expected_minutes,
            "shift_start": r.shift_start, "shift_end": r.shift_end,
        }
        for r in rows
    ]


@router.post("/{user_id}/patterns", status_code=201)
def add_pattern(
    user_id: str,
    payload: WorkingPatternIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """BR-11: a retrospective effective date triggers recalculation of the
    affected days and flags any that fall in a locked period."""
    principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    org = db.get(Organisation, principal.org_id)

    existing = db.scalars(
        select(WorkingPattern)
        .where(WorkingPattern.user_id == user_id)
        .order_by(WorkingPattern.valid_from)
    ).all()
    for pattern in existing:
        if pattern.valid_from >= payload.valid_from:
            raise HTTPException(
                400,
                "A pattern already exists from that date onwards; close it first "
                "so validity ranges do not overlap (section 12.3).",
            )
        if pattern.valid_to is None or pattern.valid_to >= payload.valid_from:
            pattern.valid_to = payload.valid_from - timedelta(days=1)

    pattern = WorkingPattern(id=new_id(), user_id=user_id, **payload.model_dump())
    db.add(pattern)
    db.flush()

    affected_locked: list[str] = []
    if payload.valid_from < date.today():
        from ..services import periods

        end = date.today()
        rules.refresh(db, org, user, payload.valid_from, end)
        for period in periods.periods_between(db, org, payload.valid_from, end):
            if period.status == "locked":
                affected_locked.append(f"{period.start_date} – {period.end_date}")

    audit.record_for(
        db, principal, request, action="working_pattern.created",
        entity_type="working_pattern", entity_id=pattern.id, after=pattern,
        note="Retrospective change" if payload.valid_from < date.today() else "",
    )
    db.commit()
    return {
        "id": pattern.id,
        "recalculated_from": payload.valid_from,
        "locked_periods_affected": affected_locked,
    }


# ---------------------------------------------------------------------------
# Credentials: invitations, PINs, QR (FR-B-03, FR-D-03, FR-D-04)
# ---------------------------------------------------------------------------


@router.post("/{user_id}/invite")
def invite(
    user_id: str,
    request: Request,
    expires_days: int = 14,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    if not user.email:
        raise HTTPException(400, "This user has no e-mail address")
    invitation = Invitation(
        id=new_id(),
        org_id=principal.org_id,
        user_id=user_id,
        token=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(days=expires_days),
    )
    db.add(invitation)
    audit.record_for(
        db, principal, request, action="user.invited", entity_type="user",
        entity_id=user_id,
    )
    db.commit()
    return {"invite_url": f"/invite/{invitation.token}", "expires_at": invitation.expires_at}


@router.post("/{user_id}/pin")
def issue_pin(
    user_id: str,
    request: Request,
    digits: int = 4,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-D-04: generated automatically, unique within the workspace, stored as
    a salted hash, shown exactly once."""
    principal.require("manage_users")
    if digits not in (4, 6):
        raise HTTPException(400, "PIN length must be 4 or 6 digits")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")

    org_user_ids = set(
        db.scalars(select(User.id).where(User.org_id == principal.org_id)).all()
    )
    existing = db.scalars(
        select(Credential).where(Credential.type == "pin")
    ).all()
    peers = [c for c in existing if c.user_id in org_user_ids and c.user_id != user_id]

    for _ in range(200):
        candidate = random_pin(digits)
        if not any(verify(candidate, c) for c in peers):
            break
    else:  # pragma: no cover - exhausted PIN space
        raise HTTPException(409, "Could not allocate a unique PIN; use 6 digits")

    hashed, salt = hash_secret(candidate)
    credential = db.scalar(
        select(Credential).where(
            Credential.user_id == user_id, Credential.type == "pin"
        )
    )
    if credential is None:
        credential = Credential(user_id=user_id, type="pin", hash=hashed, salt=salt)
        db.add(credential)
    else:
        credential.hash, credential.salt = hashed, salt
        credential.last_rotated_at = utcnow()
        credential.failed_attempts = 0
        credential.locked_until = None
    audit.record_for(
        db, principal, request, action="pin.issued", entity_type="credential",
        entity_id=user_id, note=f"{digits}-digit PIN issued",
    )
    db.commit()
    return {
        "pin": candidate,
        "notice": "Distribute securely — this value is not retrievable again.",
    }


def verify(candidate: str, credential: Credential) -> bool:
    from ..security import verify_secret

    return verify_secret(candidate, credential.hash, credential.salt)


@router.post("/{user_id}/qr")
def issue_qr(
    user_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    token = secrets.token_urlsafe(24)
    hashed, salt = hash_secret(token)
    credential = db.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.type == "qr")
    )
    if credential is None:
        credential = Credential(
            user_id=user_id, type="qr", hash=hashed, salt=salt, lookup=lookup_hash(token)
        )
        db.add(credential)
    else:
        credential.hash, credential.salt = hashed, salt
        credential.lookup = lookup_hash(token)
        credential.last_rotated_at = utcnow()
    audit.record_for(
        db, principal, request, action="qr.issued", entity_type="credential",
        entity_id=user_id,
    )
    db.commit()
    return {"qr_token": token, "notice": "Print and distribute securely."}


# ---------------------------------------------------------------------------
# Bulk import (FR-B-08)
# ---------------------------------------------------------------------------


@router.post("/import")
def bulk_import(
    body: dict,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """CSV columns: personnel_number, first_name, last_name, email, role,
    team, location, employment_start, hours_per_week. `dry_run` previews."""
    principal.require("manage_users")
    content = body.get("csv", "")
    dry_run = bool(body.get("dry_run", True))
    reader = csv.DictReader(io.StringIO(content))
    from ..models import Location, Team

    teams = {t.name.lower(): t.id for t in db.scalars(select(Team).where(Team.org_id == principal.org_id)).all()}
    locations = {
        loc.name.lower(): loc.id
        for loc in db.scalars(select(Location).where(Location.org_id == principal.org_id)).all()
    }
    seen_numbers = set(
        db.scalars(
            select(User.personnel_number).where(User.org_id == principal.org_id)
        ).all()
    )

    results = []
    created = 0
    for index, row in enumerate(reader, start=2):
        errors = []
        number = (row.get("personnel_number") or "").strip()
        if not number:
            errors.append("personnel_number is required")
        elif number in seen_numbers:
            errors.append("personnel_number already exists")
        role = (row.get("role") or "employee").strip().lower()
        if role not in ROLE_RANK:
            errors.append(f"unknown role '{role}'")
        elif ROLE_RANK[role] > ROLE_RANK[principal.role]:
            errors.append("role above your own")
        email = (row.get("email") or "").strip().lower() or None
        if role != "limited" and not email:
            errors.append("e-mail required for a login-capable user")
        team_name = (row.get("team") or "").strip().lower()
        if team_name and team_name not in teams:
            errors.append(f"unknown team '{team_name}'")
        location_name = (row.get("location") or "").strip().lower()
        if location_name and location_name not in locations:
            errors.append(f"unknown location '{location_name}'")
        try:
            start = date.fromisoformat((row.get("employment_start") or "").strip())
        except ValueError:
            errors.append("employment_start must be YYYY-MM-DD")
            start = None

        results.append({"line": index, "personnel_number": number, "errors": errors})
        if errors or dry_run:
            continue

        hours = float(row.get("hours_per_week") or 40)
        daily = int(round(hours * 60 / 5))
        user = User(
            id=new_id(), org_id=principal.org_id, personnel_number=number,
            first_name=(row.get("first_name") or "").strip(),
            last_name=(row.get("last_name") or "").strip(),
            email=email, role=role,
            team_id=teams.get(team_name), location_id=locations.get(location_name),
            employment_start=start, has_login=role != "limited",
        )
        db.add(user)
        db.flush()
        db.add(
            WorkingPattern(
                id=new_id(), user_id=user.id, valid_from=start,
                contracted_hours_per_week=hours,
                expected_minutes=[daily] * 5 + [0, 0],
            )
        )
        seen_numbers.add(number)
        created += 1

    if not dry_run and created:
        audit.record_for(
            db, principal, request, action="user.bulk_imported", entity_type="user",
            after={"created": created},
        )
        db.commit()
    else:
        db.rollback()

    return {
        "dry_run": dry_run,
        "rows": len(results),
        "valid": sum(1 for r in results if not r["errors"]),
        "created": created,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Subject access export (FR-L-05, DP-06, Art. 15 and Art. 20)
# ---------------------------------------------------------------------------


@router.get("/{user_id}/data-export")
def subject_access_export(
    user_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if user_id != principal.id:
        principal.require("manage_users")
    user = db.get(User, user_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")

    def dump(rows):
        return [audit.snapshot(r) for r in rows]

    payload = {
        "generated_at": utcnow().isoformat(),
        "subject": audit.snapshot(user),
        "working_patterns": dump(
            db.scalars(select(WorkingPattern).where(WorkingPattern.user_id == user_id)).all()
        ),
        "attendance_sessions": dump(
            db.scalars(
                select(AttendanceSession).where(AttendanceSession.user_id == user_id)
            ).all()
        ),
        "time_entries": dump(
            db.scalars(select(TimeEntry).where(TimeEntry.user_id == user_id)).all()
        ),
        "day_aggregates": dump(
            db.scalars(select(DayAggregate).where(DayAggregate.user_id == user_id)).all()
        ),
        "absence_requests": dump(
            db.scalars(select(AbsenceRequest).where(AbsenceRequest.user_id == user_id)).all()
        ),
        "exceptions": dump(
            db.scalars(
                select(AttendanceException).where(AttendanceException.user_id == user_id)
            ).all()
        ),
        "absence_balances": absence_service.all_balances(db, user),
        "kiosk_assignments": [
            k.name
            for k in db.scalars(select(Kiosk).where(Kiosk.org_id == user.org_id)).all()
            if user_id in (k.assignee_ids or [])
        ],
        "note": (
            "Credential material (password, PIN, QR token) is stored only as a "
            "salted hash and is therefore not included."
        ),
    }
    audit.record_for(
        db, principal, request, action="data_export.generated", entity_type="user",
        entity_id=user_id,
    )
    db.commit()
    return payload
