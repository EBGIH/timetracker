"""Append-only audit trail (FR-L-01 .. FR-L-03).

Nothing in the application layer updates or deletes an AuditRecord; the only
write path is `record()`. In production the database role used by the
application should additionally be denied UPDATE and DELETE on this table.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from .models import AuditRecord

_SENSITIVE = {"hash", "salt", "secret", "mfa_secret", "launch_token", "token", "lookup"}


def snapshot(obj: Any) -> dict | None:
    """Serialise a mapped object, redacting credential material (NFR-S-02:
    PINs are never logged)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        raw = obj
    else:
        try:
            mapper = inspect(obj).mapper
        except Exception:
            return {"value": str(obj)}
        raw = {c.key: getattr(obj, c.key) for c in mapper.column_attrs}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if any(token in key.lower() for token in _SENSITIVE):
            out[key] = "[redacted]"
        elif isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        elif isinstance(value, (str, int, float, bool, list, dict)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    org_id: str | None = None,
    before: Any = None,
    after: Any = None,
    ip: str | None = None,
    user_agent: str | None = None,
    note: str = "",
) -> AuditRecord:
    entry = AuditRecord(
        org_id=org_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=snapshot(before),
        after_json=snapshot(after),
        ip=ip,
        user_agent=user_agent,
        note=note,
    )
    db.add(entry)
    return entry


def record_for(db: Session, principal, request=None, **kwargs) -> AuditRecord:
    """Convenience wrapper that fills actor and request metadata."""
    ip = None
    user_agent = None
    if request is not None:
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")[:300]
    return record(
        db,
        actor_user_id=getattr(principal, "id", None),
        actor_role=getattr(principal, "role", None),
        org_id=getattr(principal, "org_id", None),
        ip=ip,
        user_agent=user_agent,
        **kwargs,
    )
