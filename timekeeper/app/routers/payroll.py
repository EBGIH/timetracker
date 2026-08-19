"""Payroll export endpoints (Module J)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import Organisation, PayrollExport, PayrollLayout, Period, new_id
from ..schemas import PayrollExportRequest, PayrollLayoutIn
from ..security import Principal, get_principal
from ..services import payroll, webhooks

router = APIRouter(prefix="/api/payroll", tags=["payroll"])


def org_of(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(404, "Organisation not found")
    return org


@router.get("/columns")
def available_columns(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    org = org_of(principal, db)
    return {
        "base": payroll.BASE_COLUMNS,
        "absence_by_policy": [column for column, _ in payroll.policy_columns(db, org)],
    }


@router.get("/layouts")
def list_layouts(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    principal.require("payroll_export")
    org = org_of(principal, db)
    payroll.default_layout(db, org)
    db.commit()
    rows = db.scalars(
        select(PayrollLayout).where(PayrollLayout.org_id == principal.org_id)
    ).all()
    return [
        {c.key: getattr(r, c.key) for c in r.__mapper__.column_attrs} for r in rows
    ]


@router.post("/layouts", status_code=201)
def create_layout(
    payload: PayrollLayoutIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-J-01: column set, order, delimiter, encoding, date and duration format."""
    principal.require("configure_policies")
    layout = PayrollLayout(
        id=new_id(), org_id=principal.org_id, is_default=False, **payload.model_dump()
    )
    db.add(layout)
    audit.record_for(
        db, principal, request, action="payroll_layout.created",
        entity_type="payroll_layout", entity_id=layout.id, after=layout,
    )
    db.commit()
    return {"id": layout.id}


@router.put("/layouts/{layout_id}")
def update_layout(
    layout_id: str,
    payload: PayrollLayoutIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    layout = db.get(PayrollLayout, layout_id)
    if layout is None or layout.org_id != principal.org_id:
        raise HTTPException(404, "Layout not found")
    before = audit.snapshot(layout)
    for field, value in payload.model_dump().items():
        setattr(layout, field, value)
    audit.record_for(
        db, principal, request, action="payroll_layout.updated",
        entity_type="payroll_layout", entity_id=layout.id, before=before, after=layout,
    )
    db.commit()
    return {"status": "ok"}


@router.post("/exports")
def generate_export(
    payload: PayrollExportRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """US-06 AC-4: exporting an unlocked period requires an explicit
    confirmation, because the data may still change."""
    principal.require("payroll_export")
    org = org_of(principal, db)
    period = db.get(Period, payload.period_id)
    if period is None or period.org_id != org.id:
        raise HTTPException(404, "Period not found")
    if period.status != "locked" and not payload.confirm_unlocked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "period_not_locked",
                "message": (
                    "This period is not locked, so the figures may still change. "
                    "Re-send with confirm_unlocked=true to export anyway."
                ),
            },
        )
    layout = (
        db.get(PayrollLayout, payload.layout_id)
        if payload.layout_id
        else payroll.default_layout(db, org)
    )
    if layout is None or layout.org_id != org.id:
        raise HTTPException(404, "Layout not found")

    previous = db.scalar(
        select(PayrollExport)
        .where(PayrollExport.period_id == period.id)
        .order_by(PayrollExport.generated_at.desc())
    )
    export = payroll.generate(db, org, period, principal.id, layout)
    reconciliation = payroll.reconcile(export, previous)

    audit.record_for(
        db, principal, request, action="payroll.exported",
        entity_type="payroll_export", entity_id=export.id,
        after={
            "period": f"{period.start_date} – {period.end_date}",
            "rows": export.row_count,
            "checksum": export.checksum,
            "layout": layout.name,
            "period_locked": export.period_locked,
        },
    )
    webhooks.emit(db, org.id, "payroll_exported",
                  {"period_id": period.id, "export_id": export.id,
                   "checksum": export.checksum})
    db.commit()
    return {
        "id": export.id,
        "row_count": export.row_count,
        "checksum": export.checksum,
        "period_locked": export.period_locked,
        "generated_at": export.generated_at,
        "reconciliation": reconciliation,
        "preview": export.content.splitlines()[:6],
    }


@router.get("/exports")
def list_exports(
    period_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("payroll_export")
    query = select(PayrollExport).where(PayrollExport.org_id == principal.org_id)
    if period_id:
        query = query.where(PayrollExport.period_id == period_id)
    rows = db.scalars(query.order_by(PayrollExport.generated_at.desc())).all()
    return [
        {
            "id": r.id, "period_id": r.period_id, "generated_at": r.generated_at,
            "generated_by": r.generated_by, "row_count": r.row_count,
            "checksum": r.checksum, "period_locked": r.period_locked,
        }
        for r in rows
    ]


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-J-04: any historic export can be re-downloaded, byte for byte."""
    principal.require("payroll_export")
    export = db.get(PayrollExport, export_id)
    if export is None or export.org_id != principal.org_id:
        raise HTTPException(404, "Export not found")
    layout = db.get(PayrollLayout, export.layout_id) if export.layout_id else None
    encoding = layout.encoding if layout else "utf-8"
    audit.record_for(
        db, principal, request, action="payroll.downloaded",
        entity_type="payroll_export", entity_id=export.id,
        after={"checksum": export.checksum},
    )
    db.commit()
    return Response(
        content=export.content.encode(encoding, errors="replace"),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="payroll_{export.id}.csv"',
            "X-Checksum-SHA256": export.checksum,
        },
    )


@router.get("/exports/{export_id}/reconciliation")
def reconciliation(
    export_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("payroll_export")
    export = db.get(PayrollExport, export_id)
    if export is None or export.org_id != principal.org_id:
        raise HTTPException(404, "Export not found")
    previous = db.scalar(
        select(PayrollExport)
        .where(
            PayrollExport.period_id == export.period_id,
            PayrollExport.generated_at < export.generated_at,
        )
        .order_by(PayrollExport.generated_at.desc())
    )
    return payroll.reconcile(export, previous)
