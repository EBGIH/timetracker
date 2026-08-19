"""Authentication, credential hashing and role-based access control.

NFR-S-03: authorisation is enforced server-side on every request. The client
never decides what a user may see; it only decides what to render.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import ApiKey, Team, User, utcnow

# ---------------------------------------------------------------------------
# Credential hashing (NFR-S-02)
# ---------------------------------------------------------------------------


def hash_secret(plain: str, salt: str | None = None) -> tuple[str, str]:
    """PBKDF2-HMAC-SHA256. Returns (hash_hex, salt_hex)."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), bytes.fromhex(salt), settings.pbkdf2_iterations
    )
    return dk.hex(), salt


def verify_secret(plain: str, hash_hex: str, salt: str) -> bool:
    candidate, _ = hash_secret(plain, salt)
    return hmac.compare_digest(candidate, hash_hex)


def lookup_hash(value: str) -> str:
    """Deterministic, keyed lookup value used to find a credential without
    storing the plaintext (e.g. QR tokens). Never reversible."""
    return hmac.new(
        settings.secret_key.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def random_pin(digits: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


# ---------------------------------------------------------------------------
# Tokens (compact JWT, HS256, no third-party dependency)
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(claims: dict, minutes: int | None = None) -> str:
    payload = dict(claims)
    exp = datetime.now(timezone.utc) + timedelta(
        minutes=minutes if minutes is not None else settings.access_token_minutes
    )
    payload["exp"] = int(exp.timestamp())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(settings.secret_key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        header, body, sig = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(
        settings.secret_key.encode(), f"{header}.{body}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_unb64(sig), expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("exp", 0) < datetime.now(timezone.utc).timestamp():
        return None
    return payload


# ---------------------------------------------------------------------------
# Permission matrix (specification section 9.1)
# ---------------------------------------------------------------------------

ALL, TEAM, SELF, CONFIG = "all", "team", "self", "config"

PERMISSIONS: dict[str, dict[str, str]] = {
    # capability                 owner  admin  hr     manager  employee limited
    "clock_self": {
        "owner": SELF, "admin": SELF, "hr": SELF,
        "manager": SELF, "employee": SELF, "limited": SELF,
    },
    "own_entry": {
        "owner": SELF, "admin": SELF, "hr": SELF,
        "manager": SELF, "employee": SELF,
    },
    "own_report": {
        "owner": SELF, "admin": SELF, "hr": SELF,
        "manager": SELF, "employee": SELF,
    },
    "view_team_attendance": {
        "owner": ALL, "admin": ALL, "hr": ALL, "manager": TEAM,
    },
    "view_all_attendance": {"owner": ALL, "admin": ALL, "hr": ALL},
    "edit_other_entry": {
        "owner": ALL, "admin": ALL, "hr": ALL, "manager": TEAM,
    },
    "approve_timesheet": {
        "owner": ALL, "admin": ALL, "hr": ALL, "manager": TEAM,
    },
    "approve_absence": {
        "owner": ALL, "admin": ALL, "hr": ALL, "manager": TEAM,
    },
    "lock_period": {"owner": ALL, "admin": ALL, "hr": ALL},
    "payroll_export": {"owner": ALL, "admin": ALL, "hr": ALL},
    "manage_users": {"owner": ALL, "admin": ALL, "hr": ALL},
    "configure_policies": {"owner": ALL, "admin": ALL, "hr": ALL},
    "manage_kiosk": {
        "owner": ALL, "admin": ALL, "hr": ALL, "manager": CONFIG,
    },
    "configure_org": {"owner": ALL, "admin": ALL},
    "manage_subscription": {"owner": ALL},
    "view_audit": {"owner": ALL, "admin": ALL, "hr": ALL},
}

# Roles for which MFA is mandatory (NFR-S-04)
MFA_MANDATORY_ROLES = {"owner", "admin", "hr"}


def capability_scope(role: str, capability: str) -> str | None:
    return PERMISSIONS.get(capability, {}).get(role)


# ---------------------------------------------------------------------------
# Request principals
# ---------------------------------------------------------------------------


class Principal:
    """The authenticated actor for one request."""

    def __init__(self, user: User, via: str = "session", scopes: list[str] | None = None):
        self.user = user
        self.via = via
        self.scopes = scopes or []

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def org_id(self) -> str:
        return self.user.org_id

    @property
    def role(self) -> str:
        return self.user.role

    def can(self, capability: str) -> bool:
        if self.via == "api_key" and self.scopes and "*" not in self.scopes:
            if capability not in self.scopes:
                return False
        return capability_scope(self.role, capability) is not None

    def scope(self, capability: str) -> str | None:
        if not self.can(capability):
            return None
        return capability_scope(self.role, capability)

    def require(self, capability: str) -> str:
        scope = self.scope(capability)
        if scope is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"Role '{self.role}' is not permitted to: {capability}",
            )
        return scope


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    token = _bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")

    # API key path (FR-J-05)
    if token.startswith("tk_"):
        parts = token.split("_")
        if len(parts) < 3:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed API key")
        prefix = parts[1]
        key = db.scalar(
            select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.revoked.is_(False))
        )
        if not key or not verify_secret(token, key.hash, key.salt):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
        key.last_used_at = utcnow()
        user = db.get(User, key.user_id)
        if not user or user.status != "active":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Inactive key owner")
        db.commit()
        return Principal(user, via="api_key", scopes=list(key.scopes or []))

    payload = decode_token(token)
    if not payload or payload.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, payload.get("sub"))
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or inactive user")
    return Principal(user)


# ---------------------------------------------------------------------------
# Visibility helpers (DP-09 least privilege)
# ---------------------------------------------------------------------------


def descendant_team_ids(db: Session, root_ids: list[str]) -> set[str]:
    """A manager's scope includes sub-teams (FR-A-02 three-level hierarchy)."""
    result: set[str] = set()
    frontier = list(root_ids)
    while frontier:
        current = frontier.pop()
        if current in result:
            continue
        result.add(current)
        children = db.scalars(
            select(Team.id).where(Team.parent_team_id == current)
        ).all()
        frontier.extend(children)
    return result


def managed_team_ids(db: Session, user: User) -> set[str]:
    roots = db.scalars(
        select(Team.id).where(
            Team.org_id == user.org_id, Team.manager_user_id == user.id
        )
    ).all()
    return descendant_team_ids(db, list(roots))


def visible_user_ids(db: Session, principal: Principal, capability: str) -> set[str] | None:
    """Returns the set of user ids the principal may see for a capability, or
    None meaning "the whole organisation"."""
    scope = principal.scope(capability)
    if scope == ALL:
        return None
    if scope == TEAM:
        team_ids = managed_team_ids(db, principal.user)
        if not team_ids:
            return {principal.id}
        ids = set(
            db.scalars(
                select(User.id).where(
                    User.org_id == principal.org_id, User.team_id.in_(team_ids)
                )
            ).all()
        )
        ids.add(principal.id)
        return ids
    if scope == SELF:
        return {principal.id}
    return set()


def assert_may_view(db: Session, principal: Principal, target_user_id: str) -> None:
    if target_user_id == principal.id:
        return
    for capability in ("view_all_attendance", "view_team_attendance"):
        if principal.can(capability):
            allowed = visible_user_ids(db, principal, capability)
            if allowed is None or target_user_id in allowed:
                return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted to view this employee")


def assert_may_edit(db: Session, principal: Principal, target_user_id: str) -> None:
    if target_user_id == principal.id:
        if principal.can("own_entry"):
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted to edit entries")
    allowed = visible_user_ids(db, principal, "edit_other_entry")
    if allowed is None or target_user_id in allowed:
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN, "Not permitted to edit this employee's records"
    )
