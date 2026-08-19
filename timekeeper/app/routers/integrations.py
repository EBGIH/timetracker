"""Public API, webhooks, calendar feed and identity provisioning (Module J)."""

from __future__ import annotations

import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AbsencePolicy,
    AbsenceRequest,
    ApiKey,
    AttendanceSession,
    Organisation,
    User,
    Webhook,
    WebhookDelivery,
    new_id,
    utcnow,
)
from ..schemas import ApiKeyIn, ReportFilters, WebhookIn
from ..security import Principal, get_principal, hash_secret
from ..services import reports as report_service, webhooks as webhook_service

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
public_api = APIRouter(prefix="/api/v1", tags=["public api"])


# ---------------------------------------------------------------------------
# API keys (FR-J-05)
# ---------------------------------------------------------------------------


@router.get("/api-keys")
def list_keys(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    principal.require("configure_org")
    rows = db.scalars(select(ApiKey).where(ApiKey.org_id == principal.org_id)).all()
    return [
        {
            "id": r.id, "name": r.name, "prefix": r.prefix, "scopes": r.scopes,
            "revoked": r.revoked, "last_used_at": r.last_used_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/api-keys", status_code=201)
def create_key(
    payload: ApiKeyIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """The key inherits the creating user's role; scopes narrow it further, they
    never widen it (NFR-S-03)."""
    principal.require("configure_org")
    prefix = secrets.token_hex(4)
    secret = f"tk_{prefix}_{secrets.token_urlsafe(32)}"
    hashed, salt = hash_secret(secret)
    key = ApiKey(
        id=new_id(), org_id=principal.org_id, user_id=principal.id, name=payload.name,
        prefix=prefix, hash=hashed, salt=salt, scopes=payload.scopes,
    )
    db.add(key)
    audit.record_for(
        db, principal, request, action="api_key.created", entity_type="api_key",
        entity_id=key.id, after={"name": payload.name, "scopes": payload.scopes},
    )
    db.commit()
    return {"id": key.id, "api_key": secret,
            "notice": "Store this now — it is not retrievable again."}


@router.post("/api-keys/{key_id}/revoke")
def revoke_key(
    key_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_org")
    key = db.get(ApiKey, key_id)
    if key is None or key.org_id != principal.org_id:
        raise HTTPException(404, "Key not found")
    key.revoked = True
    audit.record_for(
        db, principal, request, action="api_key.revoked", entity_type="api_key",
        entity_id=key.id,
    )
    db.commit()
    return {"status": "revoked"}


# ---------------------------------------------------------------------------
# Webhooks (FR-J-06)
# ---------------------------------------------------------------------------


@router.get("/webhooks")
def list_webhooks(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    principal.require("configure_org")
    rows = db.scalars(select(Webhook).where(Webhook.org_id == principal.org_id)).all()
    return {
        "available_events": list(webhook_service.EVENTS),
        "webhooks": [
            {"id": r.id, "url": r.url, "events": r.events, "active": r.active}
            for r in rows
        ],
    }


@router.post("/webhooks", status_code=201)
def create_webhook(
    payload: WebhookIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_org")
    unknown = set(payload.events) - set(webhook_service.EVENTS)
    if unknown:
        raise HTTPException(400, f"Unknown events: {', '.join(sorted(unknown))}")
    secret = secrets.token_urlsafe(32)
    hook = Webhook(
        id=new_id(), org_id=principal.org_id, url=payload.url,
        events=payload.events, secret=secret,
    )
    db.add(hook)
    audit.record_for(
        db, principal, request, action="webhook.created", entity_type="webhook",
        entity_id=hook.id, after={"url": payload.url, "events": payload.events},
    )
    db.commit()
    return {"id": hook.id, "secret": secret,
            "notice": "Verify the X-TimeKeeper-Signature header (HMAC-SHA256)."}


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(
    webhook_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_org")
    hook = db.get(Webhook, webhook_id)
    if hook is None or hook.org_id != principal.org_id:
        raise HTTPException(404, "Webhook not found")
    hook.active = False
    db.commit()
    return {"status": "disabled"}


@router.get("/webhooks/{webhook_id}/deliveries")
def webhook_deliveries(
    webhook_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_org")
    hook = db.get(Webhook, webhook_id)
    if hook is None or hook.org_id != principal.org_id:
        raise HTTPException(404, "Webhook not found")
    rows = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": r.id, "event": r.event, "status": r.status, "attempts": r.attempts,
            "response_code": r.response_code, "created_at": r.created_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Calendar feed (FR-J-07)
# ---------------------------------------------------------------------------


def _ical_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


@router.get("/calendar.ics")
def calendar_feed(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    """Approved absence as an iCal feed, scoped to what the caller may see."""
    from ..security import visible_user_ids

    allowed = None
    if principal.can("view_all_attendance"):
        allowed = None
    elif principal.can("view_team_attendance"):
        allowed = visible_user_ids(db, principal, "view_team_attendance")
    else:
        allowed = {principal.id}

    query = select(AbsenceRequest).where(
        AbsenceRequest.org_id == principal.org_id,
        AbsenceRequest.status == "approved",
        AbsenceRequest.end_date >= date.today() - timedelta(days=365),
    )
    if allowed is not None:
        query = query.where(AbsenceRequest.user_id.in_(allowed))
    rows = db.scalars(query).all()

    users = {u.id: u.display_name for u in db.scalars(
        select(User).where(User.org_id == principal.org_id)).all()}
    policies = {p.id: p.name for p in db.scalars(
        select(AbsencePolicy).where(AbsencePolicy.org_id == principal.org_id)).all()}

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TimeKeeper//Absence//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    ]
    for row in rows:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{row.id}@timekeeper",
            f"DTSTAMP:{utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{row.start_date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(row.end_date + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ical_escape(users.get(row.user_id, ''))} — "
            f"{_ical_escape(policies.get(row.policy_id, 'Absence'))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Response(
        content="\r\n".join(lines),
        media_type="text/calendar",
        headers={"Content-Disposition": 'inline; filename="absence.ics"'},
    )


# ---------------------------------------------------------------------------
# SSO / SCIM (FR-B-09) — provisioning surface
# ---------------------------------------------------------------------------


@router.get("/sso/metadata")
def sso_metadata(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The service-provider description an identity provider needs. Assertion
    consumption itself is deployment-specific and is wired to the reverse proxy
    or the identity provider's OIDC endpoints."""
    org = db.get(Organisation, principal.org_id)
    from ..config import settings

    return {
        "entity_id": f"{settings.app_base_url}/sso/{org.id}",
        "acs_url": f"{settings.app_base_url}/api/integrations/sso/acs",
        "oidc_redirect_uri": f"{settings.app_base_url}/api/integrations/sso/callback",
        "name_id_format": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        "attributes_expected": ["email", "givenName", "familyName", "employeeNumber"],
        "scim_base_url": f"{settings.app_base_url}/api/integrations/scim/v2",
        "status": "configuration required — see README, Phase 3",
    }


@router.get("/scim/v2/Users")
def scim_list_users(
    startIndex: int = 1,
    count: int = 100,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("manage_users")
    rows = db.scalars(
        select(User).where(User.org_id == principal.org_id).order_by(User.personnel_number)
    ).all()
    page = rows[startIndex - 1 : startIndex - 1 + count]
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(rows),
        "startIndex": startIndex,
        "itemsPerPage": len(page),
        "Resources": [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "id": u.id,
                "externalId": u.external_id,
                "userName": u.email or u.personnel_number,
                "name": {"givenName": u.first_name, "familyName": u.last_name},
                "emails": ([{"value": u.email, "primary": True}] if u.email else []),
                "active": u.status == "active",
            }
            for u in page
        ],
    }


# ---------------------------------------------------------------------------
# Stable public API surface (FR-J-05)
# ---------------------------------------------------------------------------


@public_api.get("/employees")
def api_employees(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    principal.require("view_all_attendance")
    rows = db.scalars(
        select(User).where(User.org_id == principal.org_id, User.status == "active")
    ).all()
    return [
        {
            "id": u.id, "personnel_number": u.personnel_number, "name": u.display_name,
            "email": u.email, "team_id": u.team_id, "location_id": u.location_id,
            "role": u.role, "employment_start": u.employment_start,
            "employment_end": u.employment_end,
        }
        for u in rows
    ]


@public_api.get("/entries")
def api_entries(
    start: date,
    end: date,
    user_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("view_all_attendance")
    from ..services import timeutil as T

    org = db.get(Organisation, principal.org_id)
    window = (
        T.local_day_bounds(start, org.timezone)[0],
        T.local_day_bounds(end, org.timezone)[1],
    )
    query = select(AttendanceSession).where(
        AttendanceSession.org_id == principal.org_id,
        AttendanceSession.start_at < window[1],
        (AttendanceSession.end_at.is_(None)) | (AttendanceSession.end_at > window[0]),
    )
    if user_id:
        query = query.where(AttendanceSession.user_id == user_id)
    return [
        {
            "id": s.id, "user_id": s.user_id, "start_at": s.start_at, "end_at": s.end_at,
            "source": s.source, "description": s.description,
            "cost_centre_id": s.cost_centre_id, "version": s.version,
            "superseded_by": s.superseded_by,
        }
        for s in db.scalars(query.order_by(AttendanceSession.start_at)).all()
    ]


@public_api.get("/absences")
def api_absences(
    start: date,
    end: date,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("view_all_attendance")
    rows = db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.org_id == principal.org_id,
            AbsenceRequest.start_date <= end,
            AbsenceRequest.end_date >= start,
        )
    ).all()
    return [
        {
            "id": r.id, "user_id": r.user_id, "policy_id": r.policy_id,
            "start_date": r.start_date, "end_date": r.end_date, "status": r.status,
            "deducted_minutes": r.deducted_minutes,
        }
        for r in rows
    ]


@public_api.post("/reports/{report_type}")
def api_report(
    report_type: str,
    filters: ReportFilters,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if report_type not in report_service.BUILDERS:
        raise HTTPException(404, "Unknown report")
    principal.require("view_all_attendance")
    org = db.get(Organisation, principal.org_id)
    result = report_service.build(db, org, principal, report_type, filters)
    db.commit()
    return result
