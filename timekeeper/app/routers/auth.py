"""Authentication, self-service profile and MFA."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..database import get_db
from ..models import Credential, Invitation, Organisation, User, utcnow
from ..schemas import LoginRequest, NotificationPrefs, PasswordChange, TokenResponse
from ..security import (
    MFA_MANDATORY_ROLES,
    PERMISSIONS,
    Principal,
    create_token,
    get_principal,
    hash_secret,
    verify_secret,
)
from ..services import totp

router = APIRouter(prefix="/api/auth", tags=["auth"])


def user_payload(db: Session, user: User) -> dict:
    org = db.get(Organisation, user.org_id)
    return {
        "id": user.id,
        "name": user.display_name,
        "email": user.email,
        "role": user.role,
        "team_id": user.team_id,
        "location_id": user.location_id,
        "personnel_number": user.personnel_number,
        "language": user.language,
        "mfa_enabled": user.mfa_enabled,
        "mfa_required": user.role in MFA_MANDATORY_ROLES,
        "capabilities": [
            name for name, roles in PERMISSIONS.items() if user.role in roles
        ],
        "organisation": {
            "id": org.id,
            "name": org.name,
            "timezone": org.timezone,
            "week_start": org.week_start,
            "duration_format": org.duration_format,
            "time_format": org.time_format,
            "date_format": org.date_format,
            "period_type": org.period_type,
            "channels": {
                "timer": org.channel_timer,
                "manual": org.channel_manual,
                "grid": org.channel_grid,
                "kiosk": org.channel_kiosk,
                "mobile": org.channel_mobile,
            },
            "require_cost_centre": org.require_cost_centre,
            "require_note": org.require_note,
            "max_session_hours": org.max_session_hours,
        }
        if org
        else None,
    }


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    generic = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if user is None or not user.has_login or user.status != "active":
        raise generic
    credential = db.scalar(
        select(Credential).where(
            Credential.user_id == user.id, Credential.type == "password"
        )
    )
    if credential is None:
        raise generic
    if credential.locked_until and credential.locked_until > utcnow():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
        )
    if not verify_secret(payload.password, credential.hash, credential.salt):
        credential.failed_attempts += 1
        if credential.failed_attempts >= settings.login_max_attempts:
            credential.locked_until = utcnow() + timedelta(
                seconds=settings.login_lockout_seconds
            )
            credential.failed_attempts = 0
        audit.record(
            db,
            action="login.failed",
            entity_type="user",
            entity_id=user.id,
            org_id=user.org_id,
            ip=request.client.host if request.client else None,
        )
        db.commit()
        raise generic

    if user.mfa_enabled:
        if not payload.mfa_code or not totp.verify(user.mfa_secret or "", payload.mfa_code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA code required or invalid")

    credential.failed_attempts = 0
    credential.locked_until = None
    audit.record(
        db,
        action="login.success",
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=user.role,
        org_id=user.org_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:300],
    )
    db.commit()
    token = create_token({"sub": user.id, "typ": "access", "role": user.role})
    return TokenResponse(access_token=token, user=user_payload(db, user))


@router.get("/me")
def me(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return user_payload(db, principal.user)


@router.post("/password")
def change_password(
    payload: PasswordChange,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    credential = db.scalar(
        select(Credential).where(
            Credential.user_id == principal.id, Credential.type == "password"
        )
    )
    if credential and payload.current_password is not None:
        if not verify_secret(payload.current_password, credential.hash, credential.salt):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    elif credential and payload.current_password is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is required")
    hashed, salt = hash_secret(payload.new_password)
    if credential is None:
        credential = Credential(user_id=principal.id, type="password", hash=hashed, salt=salt)
        db.add(credential)
    else:
        credential.hash = hashed
        credential.salt = salt
        credential.last_rotated_at = utcnow()
    audit.record_for(
        db, principal, request, action="password.changed", entity_type="user",
        entity_id=principal.id,
    )
    db.commit()
    return {"status": "ok"}


@router.post("/mfa/enrol")
def mfa_enrol(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    secret = totp.generate_secret()
    principal.user.mfa_secret = secret
    db.commit()
    return {
        "secret": secret,
        "uri": totp.provisioning_uri(secret, principal.user.email or principal.user.personnel_number),
    }


@router.post("/mfa/confirm")
def mfa_confirm(
    body: dict,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    code = str(body.get("code", ""))
    if not principal.user.mfa_secret or not totp.verify(principal.user.mfa_secret, code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    principal.user.mfa_enabled = True
    audit.record_for(
        db, principal, request, action="mfa.enabled", entity_type="user",
        entity_id=principal.id,
    )
    db.commit()
    return {"status": "enabled"}


@router.post("/mfa/disable")
def mfa_disable(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if principal.role in MFA_MANDATORY_ROLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Multi-factor authentication is mandatory for this role (NFR-S-04).",
        )
    principal.user.mfa_enabled = False
    principal.user.mfa_secret = None
    audit.record_for(
        db, principal, request, action="mfa.disabled", entity_type="user",
        entity_id=principal.id,
    )
    db.commit()
    return {"status": "disabled"}


@router.get("/invitation/{token}")
def read_invitation(token: str, db: Session = Depends(get_db)):
    invitation = db.scalar(select(Invitation).where(Invitation.token == token))
    if invitation is None or invitation.accepted_at or invitation.expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not valid")
    user = db.get(User, invitation.user_id)
    return {"email": user.email, "name": user.display_name}


@router.post("/invitation/{token}")
def accept_invitation(token: str, payload: PasswordChange, db: Session = Depends(get_db)):
    invitation = db.scalar(select(Invitation).where(Invitation.token == token))
    if invitation is None or invitation.accepted_at or invitation.expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not valid")
    hashed, salt = hash_secret(payload.new_password)
    credential = db.scalar(
        select(Credential).where(
            Credential.user_id == invitation.user_id, Credential.type == "password"
        )
    )
    if credential is None:
        db.add(
            Credential(user_id=invitation.user_id, type="password", hash=hashed, salt=salt)
        )
    else:
        credential.hash, credential.salt = hashed, salt
    invitation.accepted_at = utcnow()
    audit.record(
        db, action="invitation.accepted", entity_type="user",
        entity_id=invitation.user_id, org_id=invitation.org_id,
    )
    db.commit()
    return {"status": "ok"}


@router.put("/notification-prefs")
def set_prefs(
    payload: NotificationPrefs,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-K-03: channel and frequency per notification type, subject to a
    minimum mandatory set enforced in services.notifications."""
    principal.user.notification_prefs = payload.prefs
    db.commit()
    return {"status": "ok", "prefs": payload.prefs}
