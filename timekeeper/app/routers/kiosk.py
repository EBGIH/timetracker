"""Kiosk — the shared clock-in terminal (Module D).

The kiosk is a separate, restricted surface: it is reached with a revocable
launch token, it exposes nothing but the roster and the clock action, and it
never shows another employee's hours (FR-D-10, NFR-S-06).
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..database import get_db
from ..models import (
    AttendanceSession,
    BreakRecord,
    BreakType,
    Credential,
    Kiosk,
    Organisation,
    User,
    new_id,
    utcnow,
)
from ..schemas import KioskBatch, KioskEvent, KioskIn
from ..security import Principal, get_principal, lookup_hash, verify_secret
from ..services import periods, rules, timeutil as T, webhooks
from .attendance import apply_rounding, local_day, running_session

router = APIRouter(tags=["kiosk"])
admin = APIRouter(prefix="/api/kiosks", tags=["kiosk"])
public = APIRouter(prefix="/api/kiosk", tags=["kiosk"])


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


def may_manage_kiosk(principal: Principal, org: Organisation) -> None:
    scope = principal.require("manage_kiosk")
    if scope == "config" and not org.managers_may_launch_kiosk:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Launching a kiosk is restricted to administrators in this workspace.",
        )


@admin.get("")
def list_kiosks(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    org = db.get(Organisation, principal.org_id)
    may_manage_kiosk(principal, org)
    rows = db.scalars(select(Kiosk).where(Kiosk.org_id == principal.org_id)).all()
    return [
        {
            "id": k.id, "name": k.name, "location_id": k.location_id,
            "assignee_ids": k.assignee_ids, "auth_method": k.auth_method,
            "breaks_enabled": k.breaks_enabled, "session_hours": k.session_hours,
            "require_photo": k.require_photo, "revoked": k.revoked,
            "token_expires_at": k.token_expires_at,
            "launch_url": None if k.revoked else f"/kiosk.html?token={k.launch_token}",
        }
        for k in rows
    ]


@admin.post("", status_code=201)
def create_kiosk(
    payload: KioskIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = db.get(Organisation, principal.org_id)
    may_manage_kiosk(principal, org)
    kiosk = Kiosk(
        id=new_id(),
        org_id=principal.org_id,
        launch_token=secrets.token_urlsafe(32),
        token_expires_at=utcnow() + timedelta(hours=payload.session_hours),
        **payload.model_dump(),
    )
    db.add(kiosk)
    audit.record_for(
        db, principal, request, action="kiosk.created", entity_type="kiosk",
        entity_id=kiosk.id, after=kiosk,
    )
    db.commit()
    return {"id": kiosk.id, "launch_url": f"/kiosk.html?token={kiosk.launch_token}"}


@admin.put("/{kiosk_id}")
def update_kiosk(
    kiosk_id: str,
    payload: KioskIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = db.get(Organisation, principal.org_id)
    may_manage_kiosk(principal, org)
    kiosk = db.get(Kiosk, kiosk_id)
    if kiosk is None or kiosk.org_id != principal.org_id:
        raise HTTPException(404, "Kiosk not found")
    before = audit.snapshot(kiosk)
    for field, value in payload.model_dump().items():
        setattr(kiosk, field, value)
    audit.record_for(
        db, principal, request, action="kiosk.updated", entity_type="kiosk",
        entity_id=kiosk.id, before=before, after=kiosk,
    )
    db.commit()
    return {"status": "ok"}


@admin.post("/{kiosk_id}/relaunch")
def relaunch_kiosk(
    kiosk_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-D-08: the session expires and must be re-launched by an authorised
    user; NFR-S-06: the previous token stops working immediately."""
    org = db.get(Organisation, principal.org_id)
    may_manage_kiosk(principal, org)
    kiosk = db.get(Kiosk, kiosk_id)
    if kiosk is None or kiosk.org_id != principal.org_id:
        raise HTTPException(404, "Kiosk not found")
    kiosk.launch_token = secrets.token_urlsafe(32)
    kiosk.token_expires_at = utcnow() + timedelta(hours=kiosk.session_hours or 24)
    kiosk.revoked = False
    audit.record_for(
        db, principal, request, action="kiosk.relaunched", entity_type="kiosk",
        entity_id=kiosk.id,
    )
    db.commit()
    return {"launch_url": f"/kiosk.html?token={kiosk.launch_token}",
            "expires_at": kiosk.token_expires_at}


@admin.post("/{kiosk_id}/revoke")
def revoke_kiosk(
    kiosk_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = db.get(Organisation, principal.org_id)
    may_manage_kiosk(principal, org)
    kiosk = db.get(Kiosk, kiosk_id)
    if kiosk is None or kiosk.org_id != principal.org_id:
        raise HTTPException(404, "Kiosk not found")
    kiosk.revoked = True
    kiosk.launch_token = secrets.token_urlsafe(32)  # invalidate the leaked value
    audit.record_for(
        db, principal, request, action="kiosk.revoked", entity_type="kiosk",
        entity_id=kiosk.id,
    )
    db.commit()
    return {"status": "revoked"}


# ---------------------------------------------------------------------------
# The kiosk surface itself
# ---------------------------------------------------------------------------


def resolve_kiosk(db: Session, token: str) -> Kiosk:
    kiosk = db.scalar(select(Kiosk).where(Kiosk.launch_token == token))
    if kiosk is None or kiosk.revoked:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This kiosk link is not valid")
    if kiosk.token_expires_at and kiosk.token_expires_at < utcnow():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "This kiosk session has expired and must be re-launched by an administrator.",
        )
    return kiosk


def authenticate(db: Session, kiosk: Kiosk, user_id: str, pin: str | None, qr: str | None) -> User:
    """US-01 AC-3: a generic error that does not reveal whether the name or the
    credential was wrong, with lockout after repeated failures."""
    generic = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not recognised — please try again")
    user = db.get(User, user_id)
    if user is None or user.org_id != kiosk.org_id or user.status != "active":
        raise generic
    if user_id not in (kiosk.assignee_ids or []):
        raise generic

    if qr:
        credential = db.scalar(
            select(Credential).where(
                Credential.type == "qr", Credential.lookup == lookup_hash(qr)
            )
        )
        if credential is None or credential.user_id != user_id:
            raise generic
        return user

    credential = db.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.type == "pin")
    )
    if credential is None or not pin:
        raise generic
    if credential.locked_until and credential.locked_until > utcnow():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Locked for a few minutes after repeated failed attempts. "
            "Ask your supervisor if you need to clock in now.",
        )
    if not verify_secret(pin, credential.hash, credential.salt):
        credential.failed_attempts += 1
        if credential.failed_attempts >= settings.kiosk_max_attempts:
            credential.locked_until = utcnow() + timedelta(
                seconds=settings.kiosk_lockout_seconds
            )
            credential.failed_attempts = 0
            audit.record(
                db, action="kiosk.locked_out", entity_type="user", entity_id=user_id,
                org_id=kiosk.org_id, note=f"kiosk={kiosk.name}",
            )
        db.commit()
        raise generic
    credential.failed_attempts = 0
    credential.locked_until = None
    return user


@public.get("/session")
def kiosk_session(token: str = Query(...), db: Session = Depends(get_db)):
    """FR-D-05/06/10: the roster, each person's current clock status, and
    nothing else."""
    kiosk = resolve_kiosk(db, token)
    org = db.get(Organisation, kiosk.org_id)
    roster = []
    for user_id in kiosk.assignee_ids or []:
        user = db.get(User, user_id)
        if user is None or user.status != "active":
            continue
        session = running_session(db, user_id)
        on_break = bool(
            session and any(b.end_at is None for b in session.breaks)
        )
        roster.append(
            {
                "id": user.id,
                "name": user.display_name,
                "status": "on_break" if on_break else ("in" if session else "out"),
                "since": (session.start_at if session else None),
            }
        )
    roster.sort(key=lambda r: r["name"])
    break_types = [
        {"id": b.id, "name": b.name, "is_paid": b.is_paid}
        for b in db.scalars(select(BreakType).where(BreakType.org_id == kiosk.org_id)).all()
    ]
    return {
        "kiosk": {
            "id": kiosk.id,
            "name": kiosk.name,
            "auth_method": kiosk.auth_method,
            "breaks_enabled": kiosk.breaks_enabled,
            "require_photo": kiosk.require_photo,
            "expires_at": kiosk.token_expires_at,
        },
        "organisation": {"name": org.name if org else "", "timezone": org.timezone if org else "UTC"},
        "roster": roster,
        "break_types": break_types,
        "server_time": utcnow(),
    }


def _apply_event(db: Session, kiosk: Kiosk, org: Organisation, event: KioskEvent) -> dict:
    user = authenticate(db, kiosk, event.user_id, event.pin, event.qr_token)

    # FR-D-09: idempotency so a replayed offline event cannot double-book.
    if event.idempotency_key:
        existing = db.scalar(
            select(AttendanceSession).where(
                AttendanceSession.idempotency_key == event.idempotency_key
            )
        )
        if existing:
            return {"status": "duplicate_ignored", "session_id": existing.id,
                    "user_name": user.display_name}

    occurred = T.naive_utc(event.occurred_at) or utcnow()
    if occurred > utcnow() + timedelta(minutes=5):
        raise HTTPException(400, "Event timestamp is in the future")
    session = running_session(db, user.id)

    if event.action == "clock_in":
        if session:
            raise HTTPException(409, {"error": "already_clocked_in",
                                      "message": "You are already clocked in."})
        moment = apply_rounding(org, occurred)
        day = local_day(db, org, user, moment)
        periods.assert_editable(db, org, user.id, day, _KioskPrincipal(user))
        new_session = AttendanceSession(
            id=new_id(), org_id=org.id, user_id=user.id, start_at=moment,
            raw_start_at=occurred, source="kiosk", device_id=event.device_id,
            location_id=kiosk.location_id or user.location_id, status="open",
            created_by=user.id, idempotency_key=event.idempotency_key,
        )
        db.add(new_session)
        db.flush()
        audit.record(
            db, action="attendance.clock_in", entity_type="attendance_session",
            entity_id=new_session.id, actor_user_id=user.id, actor_role=user.role,
            org_id=org.id, after=new_session, note=f"kiosk={kiosk.name}",
        )
        webhooks.emit(db, org.id, "clock_in", {"user_id": user.id, "session_id": new_session.id})
        rules.refresh(db, org, user, day, day)
        return {"status": "clocked_in", "user_name": user.display_name,
                "at": moment, "session_id": new_session.id}

    if event.action == "clock_out":
        if session is None:
            raise HTTPException(409, {"error": "not_clocked_in",
                                      "message": "You are not clocked in."})
        moment = apply_rounding(org, occurred)
        if moment <= session.start_at:
            moment = session.start_at + timedelta(minutes=1)
        for brk in session.breaks:
            if brk.end_at is None:
                brk.end_at = moment
        before = audit.snapshot(session)
        session.end_at = moment
        session.raw_end_at = occurred
        session.status = "closed"
        audit.record(
            db, action="attendance.clock_out", entity_type="attendance_session",
            entity_id=session.id, actor_user_id=user.id, actor_role=user.role,
            org_id=org.id, before=before, after=session, note=f"kiosk={kiosk.name}",
        )
        webhooks.emit(db, org.id, "clock_out", {"user_id": user.id, "session_id": session.id})
        day = local_day(db, org, user, session.start_at)
        rules.refresh(db, org, user, day, local_day(db, org, user, moment))
        worked = int((session.end_at - session.start_at).total_seconds() // 60)
        return {"status": "clocked_out", "user_name": user.display_name,
                "at": moment, "worked_minutes": worked}

    if event.action == "break_start":
        if not kiosk.breaks_enabled:
            raise HTTPException(400, "Breaks are not enabled on this kiosk")
        if session is None:
            raise HTTPException(409, {"error": "not_clocked_in",
                                      "message": "Clock in before starting a break."})
        if any(b.end_at is None for b in session.breaks):
            raise HTTPException(409, "A break is already running")
        is_paid = False
        if event.break_type_id:
            break_type = db.get(BreakType, event.break_type_id)
            if break_type is None or break_type.org_id != org.id:
                raise HTTPException(404, "Break type not found")
            is_paid = break_type.is_paid
        record = BreakRecord(
            id=new_id(), session_id=session.id, break_type_id=event.break_type_id,
            start_at=max(occurred, session.start_at), is_paid=is_paid,
        )
        db.add(record)
        audit.record(
            db, action="break.started", entity_type="break_record", entity_id=record.id,
            actor_user_id=user.id, org_id=org.id, after=record, note=f"kiosk={kiosk.name}",
        )
        return {"status": "break_started", "user_name": user.display_name, "at": record.start_at}

    # break_end
    if session is None:
        raise HTTPException(409, "You are not clocked in.")
    record = next((b for b in session.breaks if b.end_at is None), None)
    if record is None:
        raise HTTPException(409, "No break is running")
    record.end_at = max(occurred, record.start_at + timedelta(minutes=1))
    audit.record(
        db, action="break.ended", entity_type="break_record", entity_id=record.id,
        actor_user_id=user.id, org_id=org.id, after=record, note=f"kiosk={kiosk.name}",
    )
    day = local_day(db, org, user, record.start_at)
    rules.refresh(db, org, user, day, day)
    return {"status": "break_ended", "user_name": user.display_name, "at": record.end_at}


class _KioskPrincipal:
    """A minimal principal so period rules apply identically at the kiosk."""

    def __init__(self, user: User):
        self.user = user
        self.id = user.id
        self.org_id = user.org_id
        self.role = user.role

    def can(self, capability: str) -> bool:
        from ..security import capability_scope

        return capability_scope(self.role, capability) is not None


@public.post("/event")
def kiosk_event(
    payload: KioskEvent, token: str = Query(...), db: Session = Depends(get_db)
):
    kiosk = resolve_kiosk(db, token)
    org = db.get(Organisation, kiosk.org_id)
    result = _apply_event(db, kiosk, org, payload)
    db.commit()
    return result


@public.post("/sync")
def kiosk_sync(
    payload: KioskBatch, token: str = Query(...), db: Session = Depends(get_db)
):
    """FR-D-09 / US-01 AC-5: events captured offline are replayed with their
    original timestamps; idempotency keys make the replay safe."""
    kiosk = resolve_kiosk(db, token)
    org = db.get(Organisation, kiosk.org_id)
    results = []
    for event in sorted(payload.events, key=lambda e: e.occurred_at or utcnow()):
        try:
            results.append({"idempotency_key": event.idempotency_key,
                            "ok": True, **_apply_event(db, kiosk, org, event)})
        except HTTPException as exc:
            results.append(
                {"idempotency_key": event.idempotency_key, "ok": False,
                 "error": exc.detail}
            )
    db.commit()
    return {"results": results}


router.include_router(admin)
router.include_router(public)
