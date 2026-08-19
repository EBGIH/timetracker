"""Reporting endpoints (Module I)."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import Organisation, SavedReport, User, new_id, utcnow
from ..schemas import ReportFilters, SavedReportIn, ShareRequest
from ..security import Principal, get_principal
from ..services import exports, reports

router = APIRouter(prefix="/api/reports", tags=["reports"])


def org_of(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(404, "Organisation not found")
    return org


@router.get("/catalogue")
def catalogue(principal: Principal = Depends(get_principal)):
    """Section 17 — the reporting catalogue, filtered to what this role may run."""
    entries = [
        {"type": "live_board", "title": "Live team board", "grain": "Real time",
         "primary_user": "Manager", "requires": "view_team_attendance"},
        {"type": "attendance", "title": "Attendance report", "grain": "Employee × day",
         "primary_user": "HR, Payroll", "requires": "own_report"},
        {"type": "weekly", "title": "Weekly report", "grain": "Employee × week",
         "primary_user": "Manager", "requires": "own_report"},
        {"type": "summary", "title": "Summary report", "grain": "Configurable grouping",
         "primary_user": "Management", "requires": "view_team_attendance"},
        {"type": "detailed", "title": "Detailed report", "grain": "Individual entry",
         "primary_user": "HR, Audit", "requires": "own_report"},
        {"type": "absence", "title": "Absence report", "grain": "Employee × policy",
         "primary_user": "HR", "requires": "own_report"},
        {"type": "overtime", "title": "Overtime report", "grain": "Employee × category",
         "primary_user": "Payroll, Management", "requires": "view_team_attendance"},
        {"type": "compliance", "title": "Compliance report", "grain": "Exception",
         "primary_user": "HR, Legal", "requires": "view_team_attendance"},
        {"type": "exception_queue", "title": "Exception queue", "grain": "Exception",
         "primary_user": "Manager, HR", "requires": "view_team_attendance"},
    ]
    return [e for e in entries if principal.can(e["requires"])]


def _guard(principal: Principal, report_type: str) -> None:
    team_only = {"live_board", "summary", "overtime", "compliance", "exception_queue"}
    if report_type in team_only and not principal.can("view_team_attendance"):
        raise HTTPException(403, "This report requires team visibility")
    if not principal.can("own_report"):
        raise HTTPException(403, "Reporting is not available to this role")


@router.post("/run/{report_type}")
def run_report(
    report_type: str,
    filters: ReportFilters,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if report_type not in reports.BUILDERS:
        raise HTTPException(404, f"Unknown report '{report_type}'")
    _guard(principal, report_type)
    org = org_of(principal, db)
    if (filters.end - filters.start).days > 400:
        raise HTTPException(400, "The date range may not exceed 400 days")
    report = reports.build(db, org, principal, report_type, filters)
    db.commit()
    report["duration_format"] = org.duration_format
    return report


@router.post("/run/{report_type}/export")
def export_report(
    report_type: str,
    filters: ReportFilters,
    request: Request,
    fmt: str = Query("csv", pattern="^(csv|xlsx|pdf)$"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if report_type not in reports.BUILDERS:
        raise HTTPException(404, f"Unknown report '{report_type}'")
    _guard(principal, report_type)
    org = org_of(principal, db)
    report = reports.build(db, org, principal, report_type, filters)
    payload = exports.export(report, fmt, org.duration_format, org.name)
    audit.record_for(
        db, principal, request, action="report.exported", entity_type="report",
        entity_id=report_type,
        after={"format": fmt, "rows": len(report["rows"]),
               "start": filters.start.isoformat(), "end": filters.end.isoformat()},
    )
    db.commit()
    filename = f"{report_type}_{filters.start}_{filters.end}.{fmt}"
    return Response(
        content=payload,
        media_type=exports.MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Saved, scheduled and shared reports (FR-I-09, FR-I-11, FR-I-12)
# ---------------------------------------------------------------------------


@router.get("/saved/list")
def list_saved(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(SavedReport).where(
            SavedReport.org_id == principal.org_id,
            SavedReport.owner_id == principal.id,
        )
    ).all()
    return [
        {
            "id": r.id, "name": r.name, "report_type": r.report_type,
            "filters": r.filters, "schedule_cron": r.schedule_cron,
            "schedule_recipients": r.schedule_recipients,
            "last_sent_at": r.last_sent_at,
            "share_url": (
                f"/shared/{r.share_token}" if r.share_token
                and (r.share_expires_at is None or r.share_expires_at > utcnow())
                else None
            ),
            "share_expires_at": r.share_expires_at,
        }
        for r in rows
    ]


@router.post("/saved", status_code=201)
def save_report(
    payload: SavedReportIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if payload.report_type not in reports.BUILDERS:
        raise HTTPException(400, "Unknown report type")
    record = SavedReport(
        id=new_id(), org_id=principal.org_id, owner_id=principal.id,
        **payload.model_dump(),
    )
    db.add(record)
    audit.record_for(
        db, principal, request, action="report.saved", entity_type="saved_report",
        entity_id=record.id, after=record,
    )
    db.commit()
    return {"id": record.id}


@router.delete("/saved/{report_id}")
def delete_saved(
    report_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    record = db.get(SavedReport, report_id)
    if record is None or record.owner_id != principal.id:
        raise HTTPException(404, "Saved report not found")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}


@router.post("/saved/{report_id}/share")
def share_report(
    report_id: str,
    payload: ShareRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-I-12: a link with an expiry that exposes nothing beyond the report's
    own scope — the link is bound to the saving user's visibility."""
    record = db.get(SavedReport, report_id)
    if record is None or record.owner_id != principal.id:
        raise HTTPException(404, "Saved report not found")
    record.share_token = secrets.token_urlsafe(24)
    record.share_expires_at = utcnow() + timedelta(days=payload.expires_in_days)
    audit.record_for(
        db, principal, request, action="report.shared", entity_type="saved_report",
        entity_id=record.id, after={"expires_at": record.share_expires_at.isoformat()},
    )
    db.commit()
    return {"share_url": f"/shared/{record.share_token}",
            "expires_at": record.share_expires_at}


@router.post("/saved/{report_id}/unshare")
def unshare_report(
    report_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    record = db.get(SavedReport, report_id)
    if record is None or record.owner_id != principal.id:
        raise HTTPException(404, "Saved report not found")
    record.share_token = None
    record.share_expires_at = None
    db.commit()
    return {"status": "unshared"}


shared = APIRouter(prefix="/api/shared", tags=["reports"])


class _OwnerPrincipal:
    """A share link runs the report with the visibility of the person who
    created it — never wider."""

    def __init__(self, user: User):
        self.user = user
        self.id = user.id
        self.org_id = user.org_id
        self.role = user.role

    def can(self, capability: str) -> bool:
        from ..security import capability_scope

        return capability_scope(self.role, capability) is not None

    def scope(self, capability: str):
        from ..security import capability_scope

        return capability_scope(self.role, capability)


@shared.get("/{token}")
def read_shared(token: str, db: Session = Depends(get_db)):
    record = db.scalar(select(SavedReport).where(SavedReport.share_token == token))
    if record is None:
        raise HTTPException(404, "This link is not valid")
    if record.share_expires_at and record.share_expires_at < utcnow():
        raise HTTPException(410, "This link has expired")
    owner = db.get(User, record.owner_id)
    org = db.get(Organisation, record.org_id)
    if owner is None or owner.status != "active":
        raise HTTPException(410, "The owner of this link is no longer active")
    filters = ReportFilters(**record.filters)
    report = reports.build(db, org, _OwnerPrincipal(owner), record.report_type, filters)
    db.commit()
    report["shared"] = True
    report["duration_format"] = org.duration_format
    return report
