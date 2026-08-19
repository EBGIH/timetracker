"""Payroll export and reconciliation (Module J).

BR-10: where the organisation requires prior approval of overtime, only
approved overtime reaches payroll. Unapproved overtime is still recorded and
still reported — it is simply excluded from the export until approved.
"""

from __future__ import annotations

import hashlib
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AbsencePolicy,
    AbsenceRequest,
    DayAggregate,
    Organisation,
    Period,
    PayrollExport,
    PayrollLayout,
    User,
    new_id,
)
from . import calc, timeutil as T

BASE_COLUMNS = [
    "personnel_number",
    "employee",
    "period_start",
    "period_end",
    "expected_minutes",
    "normal_minutes",
    "overtime_standard",
    "overtime_night",
    "overtime_weekend",
    "overtime_holiday",
    "overtime_total",
    "overtime_unapproved",
    "absence_paid_minutes",
    "absence_unpaid_minutes",
    "unpaid_break_minutes",
    "night_minutes",
    "balance_minutes",
]


def default_layout(db: Session, org: Organisation) -> PayrollLayout:
    layout = db.scalar(
        select(PayrollLayout).where(
            PayrollLayout.org_id == org.id, PayrollLayout.is_default.is_(True)
        )
    )
    if layout is None:
        layout = PayrollLayout(
            id=new_id(),
            org_id=org.id,
            name="Default delimited layout",
            columns=[
                "personnel_number", "period_start", "period_end", "normal_minutes",
                "overtime_standard", "overtime_night", "overtime_weekend",
                "overtime_holiday", "absence_paid_minutes", "absence_unpaid_minutes",
                "unpaid_break_minutes",
            ],
        )
        db.add(layout)
        db.flush()
    return layout


def policy_columns(db: Session, org: Organisation) -> list[tuple[str, AbsencePolicy]]:
    """FR-J-02: paid absence broken down by type."""
    out = []
    for policy in db.scalars(
        select(AbsencePolicy).where(AbsencePolicy.org_id == org.id)
    ).all():
        code = (policy.code or policy.name).lower().replace(" ", "_")
        out.append((f"absence_{code}_minutes", policy))
    return out


def build_rows(db: Session, org: Organisation, period: Period) -> list[dict]:
    from .periods import ensure_approval

    users = db.scalars(
        select(User).where(User.org_id == org.id).order_by(User.personnel_number)
    ).all()
    extra_columns = policy_columns(db, org)
    rows: list[dict] = []

    for user in users:
        approval = ensure_approval(db, period, user.id)
        if approval.excluded:
            continue
        if user.employment_end and user.employment_end < period.start_date:
            continue
        if user.employment_start and user.employment_start > period.end_date:
            continue

        totals = calc.period_totals(db, user.id, period.start_date, period.end_date)
        if totals["days"] == 0 and user.status != "active":
            continue

        overtime_total = totals["overtime_total"]
        approved = totals["overtime_approved_minutes"]
        # Scale each category proportionally to the approved share (BR-10).
        share = (approved / overtime_total) if overtime_total else 0
        payable = {
            key: int(round(totals[key] * share))
            for key in ("overtime_standard", "overtime_night", "overtime_weekend",
                        "overtime_holiday")
        }
        payable_total = sum(payable.values())
        normal = max(0, totals["net_worked_minutes"] - overtime_total)

        row = {
            "user_id": user.id,
            "personnel_number": user.personnel_number,
            "employee": user.display_name,
            "period_start": period.start_date.isoformat(),
            "period_end": period.end_date.isoformat(),
            "expected_minutes": totals["expected_minutes"],
            "normal_minutes": normal,
            "overtime_total": payable_total,
            "overtime_unapproved": overtime_total - approved,
            "absence_paid_minutes": totals["absence_paid_minutes"],
            "absence_unpaid_minutes": totals["absence_minutes"] - totals["absence_paid_minutes"],
            "unpaid_break_minutes": totals["break_unpaid_minutes"],
            "night_minutes": totals["night_minutes"],
            "balance_minutes": totals["balance_minutes"],
            **payable,
        }

        for column, policy in extra_columns:
            minutes = 0
            for request in db.scalars(
                select(AbsenceRequest).where(
                    AbsenceRequest.user_id == user.id,
                    AbsenceRequest.policy_id == policy.id,
                    AbsenceRequest.status == "approved",
                    AbsenceRequest.start_date <= period.end_date,
                    AbsenceRequest.end_date >= period.start_date,
                )
            ).all():
                for day in T.daterange(
                    max(request.start_date, period.start_date),
                    min(request.end_date, period.end_date),
                ):
                    aggregate = db.scalar(
                        select(DayAggregate).where(
                            DayAggregate.user_id == user.id, DayAggregate.day == day
                        )
                    )
                    if aggregate:
                        minutes += aggregate.absence_minutes
            row[column] = minutes
        rows.append(row)
    return rows


def format_duration(minutes: int, fmt: str) -> str:
    if fmt == "minutes":
        return str(int(minutes))
    if fmt == "hm":
        return T.format_duration(int(minutes), "hm")
    return f"{minutes / 60:.2f}"


def render(rows: list[dict], layout: PayrollLayout) -> str:
    buffer = io.StringIO()
    columns = list(layout.columns or [])
    if layout.include_header:
        buffer.write(layout.delimiter.join(columns) + "\n")
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column, "")
            if column.endswith("_minutes") or column.startswith("overtime"):
                value = format_duration(int(value or 0), layout.duration_format)
            values.append(str(value))
        buffer.write(layout.delimiter.join(values) + "\n")
    return buffer.getvalue()


def generate(
    db: Session, org: Organisation, period: Period, actor_id: str,
    layout: PayrollLayout | None = None,
) -> PayrollExport:
    layout = layout or default_layout(db, org)
    rows = build_rows(db, org, period)
    content = render(rows, layout)
    checksum = hashlib.sha256(content.encode(layout.encoding or "utf-8")).hexdigest()
    export = PayrollExport(
        id=new_id(),
        org_id=org.id,
        period_id=period.id,
        layout_id=layout.id,
        generated_by=actor_id,
        row_count=len(rows),
        checksum=checksum,
        content=content,
        rows_json=rows,
        period_locked=period.status == "locked",
    )
    db.add(export)
    db.flush()
    return export


def reconcile(current: PayrollExport, previous: PayrollExport | None) -> dict:
    """FR-J-03 / US-06 AC-3: every employee whose figures changed, with the
    previous and the new values."""
    if previous is None:
        return {
            "previous_export_id": None,
            "changes": [],
            "added": [r["personnel_number"] for r in current.rows_json],
            "removed": [],
            "note": "First export for this period — nothing to compare against.",
        }
    previous_by_key = {r["personnel_number"]: r for r in previous.rows_json}
    current_by_key = {r["personnel_number"]: r for r in current.rows_json}
    numeric = [
        c for c in BASE_COLUMNS
        if c.endswith("_minutes") or c.startswith("overtime")
    ]
    changes = []
    for key, row in current_by_key.items():
        old = previous_by_key.get(key)
        if old is None:
            continue
        deltas = {}
        for column in set(numeric) | {c for c in row if c.startswith("absence_")}:
            before = int(old.get(column, 0) or 0)
            after = int(row.get(column, 0) or 0)
            if before != after:
                deltas[column] = {"previous": before, "current": after,
                                  "delta": after - before}
        if deltas:
            changes.append(
                {"personnel_number": key, "employee": row.get("employee", ""),
                 "fields": deltas}
            )
    return {
        "previous_export_id": previous.id,
        "previous_generated_at": previous.generated_at.isoformat(),
        "changes": changes,
        "added": sorted(set(current_by_key) - set(previous_by_key)),
        "removed": sorted(set(previous_by_key) - set(current_by_key)),
    }
