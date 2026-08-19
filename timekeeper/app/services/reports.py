"""Reporting layer (Module I, catalogue in section 17).

Every report returns the same envelope — columns, rows, totals, meta — so the
UI, the exporters and the scheduled-delivery job all consume one shape.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AbsencePolicy,
    AbsenceRequest,
    AttendanceException,
    AttendanceSession,
    CostCentre,
    DayAggregate,
    Location,
    Organisation,
    Team,
    TimeEntry,
    User,
)
from ..security import Principal, visible_user_ids
from . import absence as absence_service, calc, timeutil as T

REPORT_TYPES = (
    "attendance", "summary", "weekly", "detailed", "absence", "overtime",
    "compliance", "live_board", "exception_queue",
)


# ---------------------------------------------------------------------------
# Population resolution (DP-09)
# ---------------------------------------------------------------------------


def resolve_population(db: Session, principal: Principal, filters) -> list[User]:
    allowed: set[str] | None
    if principal.can("view_all_attendance"):
        allowed = None
    elif principal.can("view_team_attendance"):
        allowed = visible_user_ids(db, principal, "view_team_attendance")
    else:
        allowed = {principal.id}

    query = select(User).where(User.org_id == principal.org_id)
    if allowed is not None:
        query = query.where(User.id.in_(allowed))
    if getattr(filters, "user_ids", None):
        query = query.where(User.id.in_(filters.user_ids))
    if getattr(filters, "team_ids", None):
        from ..security import descendant_team_ids

        query = query.where(User.team_id.in_(descendant_team_ids(db, list(filters.team_ids))))
    if getattr(filters, "location_ids", None):
        query = query.where(User.location_id.in_(filters.location_ids))
    return list(db.scalars(query.order_by(User.last_name, User.first_name)).all())


def _lookup(db: Session, org_id: str) -> dict:
    return {
        "teams": {t.id: t.name for t in db.scalars(select(Team).where(Team.org_id == org_id)).all()},
        "locations": {
            loc.id: loc.name
            for loc in db.scalars(select(Location).where(Location.org_id == org_id)).all()
        },
        "cost_centres": {
            c.id: f"{c.code} {c.name}".strip()
            for c in db.scalars(select(CostCentre).where(CostCentre.org_id == org_id)).all()
        },
        "policies": {
            p.id: p.name
            for p in db.scalars(select(AbsencePolicy).where(AbsencePolicy.org_id == org_id)).all()
        },
    }


def _sum(rows: list[dict], keys: list[str]) -> dict:
    return {key: sum(r.get(key) or 0 for r in rows) for key in keys}


# ---------------------------------------------------------------------------
# FR-I-01 Attendance report
# ---------------------------------------------------------------------------


def attendance_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    names = _lookup(db, org.id)
    rows: list[dict] = []
    exceptions_by_key: dict[tuple[str, date], list[str]] = {}
    for record in db.scalars(
        select(AttendanceException).where(
            AttendanceException.org_id == org.id,
            AttendanceException.day >= filters.start,
            AttendanceException.day <= filters.end,
            AttendanceException.status == "open",
        )
    ).all():
        exceptions_by_key.setdefault((record.user_id, record.day), []).append(record.type)

    absence_names: dict[tuple[str, date], str] = {}
    for request in db.scalars(
        select(AbsenceRequest).where(
            AbsenceRequest.org_id == org.id,
            AbsenceRequest.status == "approved",
            AbsenceRequest.start_date <= filters.end,
            AbsenceRequest.end_date >= filters.start,
        )
    ).all():
        for day in T.daterange(
            max(request.start_date, filters.start), min(request.end_date, filters.end)
        ):
            absence_names[(request.user_id, day)] = names["policies"].get(request.policy_id, "")

    for user in users:
        tzname = calc.user_timezone(db, org, user)
        aggregates = {
            a.day: a
            for a in db.scalars(
                select(DayAggregate).where(
                    DayAggregate.user_id == user.id,
                    DayAggregate.day >= filters.start,
                    DayAggregate.day <= filters.end,
                )
            ).all()
        }
        for day in T.daterange(filters.start, filters.end):
            if user.employment_start and day < user.employment_start:
                continue
            if user.employment_end and day > user.employment_end:
                continue
            aggregate = aggregates.get(day)
            if aggregate is None:
                calc.persist_day(db, org, user, day)
                aggregate = db.scalar(
                    select(DayAggregate).where(
                        DayAggregate.user_id == user.id, DayAggregate.day == day
                    )
                )
            flags = exceptions_by_key.get((user.id, day), [])
            # US-06 AC-1: days with no attendance and no absence still appear.
            if (
                aggregate.present_minutes == 0
                and aggregate.absence_minutes == 0
                and aggregate.expected_minutes > 0
                and "UNEXPLAINED_ABSENCE" not in flags
            ):
                flags = flags + ["NO_RECORD"]
            row = {
                "user_id": user.id,
                "personnel_number": user.personnel_number,
                "employee": user.display_name,
                "team": names["teams"].get(user.team_id, ""),
                "location": names["locations"].get(user.location_id, ""),
                "date": day.isoformat(),
                "weekday": day.strftime("%a"),
                "first_in": (
                    T.to_local(aggregate.first_in, tzname).strftime("%H:%M")
                    if aggregate.first_in else ""
                ),
                "last_out": (
                    T.to_local(aggregate.last_out, tzname).strftime("%H:%M")
                    if aggregate.last_out else ""
                ),
                "present_minutes": aggregate.present_minutes,
                "break_minutes": aggregate.break_paid_minutes + aggregate.break_unpaid_minutes,
                "net_worked_minutes": aggregate.net_worked_minutes,
                "expected_minutes": aggregate.expected_minutes,
                "difference_minutes": aggregate.balance_minutes,
                "overtime_standard": aggregate.overtime_standard,
                "overtime_night": aggregate.overtime_night,
                "overtime_weekend": aggregate.overtime_weekend,
                "overtime_holiday": aggregate.overtime_holiday,
                "night_minutes": aggregate.night_minutes,
                "absence_minutes": aggregate.absence_minutes,
                "absence_type": absence_names.get((user.id, day), ""),
                "is_holiday": aggregate.is_holiday,
                "exceptions": ", ".join(sorted(flags)),
            }
            if filters.only_exceptions and not flags:
                continue
            rows.append(row)

    numeric = [
        "present_minutes", "break_minutes", "net_worked_minutes", "expected_minutes",
        "difference_minutes", "overtime_standard", "overtime_night",
        "overtime_weekend", "overtime_holiday", "night_minutes", "absence_minutes",
    ]
    return {
        "type": "attendance",
        "title": "Attendance report",
        "columns": [
            {"key": "personnel_number", "label": "Personnel no."},
            {"key": "employee", "label": "Employee"},
            {"key": "team", "label": "Team"},
            {"key": "date", "label": "Date"},
            {"key": "weekday", "label": "Day"},
            {"key": "first_in", "label": "First in"},
            {"key": "last_out", "label": "Last out"},
            {"key": "present_minutes", "label": "Present", "type": "duration"},
            {"key": "break_minutes", "label": "Breaks", "type": "duration"},
            {"key": "net_worked_minutes", "label": "Net worked", "type": "duration"},
            {"key": "expected_minutes", "label": "Expected", "type": "duration"},
            {"key": "difference_minutes", "label": "Difference", "type": "duration"},
            {"key": "overtime_standard", "label": "OT standard", "type": "duration"},
            {"key": "overtime_night", "label": "OT night", "type": "duration"},
            {"key": "overtime_weekend", "label": "OT weekend", "type": "duration"},
            {"key": "overtime_holiday", "label": "OT holiday", "type": "duration"},
            {"key": "absence_minutes", "label": "Absence", "type": "duration"},
            {"key": "absence_type", "label": "Absence type"},
            {"key": "exceptions", "label": "Exceptions"},
        ],
        "rows": rows,
        "totals": _sum(rows, numeric),
        "meta": {"start": filters.start, "end": filters.end, "employees": len(users)},
    }


# ---------------------------------------------------------------------------
# FR-I-02 Summary report
# ---------------------------------------------------------------------------


def summary_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    names = _lookup(db, org.id)
    group_by = (filters.group_by or "employee").lower()
    buckets: dict[str, dict] = {}
    headcount: dict[str, set[str]] = {}

    for user in users:
        aggregates = db.scalars(
            select(DayAggregate).where(
                DayAggregate.user_id == user.id,
                DayAggregate.day >= filters.start,
                DayAggregate.day <= filters.end,
            )
        ).all()
        for aggregate in aggregates:
            if group_by == "team":
                key = names["teams"].get(user.team_id, "— no team —")
            elif group_by == "location":
                key = names["locations"].get(user.location_id, "— no location —")
            elif group_by == "date":
                key = aggregate.day.isoformat()
            elif group_by == "week":
                key = T.iso_week_bounds(aggregate.day, org.week_start)[0].isoformat()
            else:
                key = user.display_name
            bucket = buckets.setdefault(
                key,
                {
                    "group": key, "net_worked_minutes": 0, "expected_minutes": 0,
                    "absence_minutes": 0, "overtime_minutes": 0, "present_minutes": 0,
                    "balance_minutes": 0,
                },
            )
            bucket["net_worked_minutes"] += aggregate.net_worked_minutes
            bucket["present_minutes"] += aggregate.present_minutes
            bucket["expected_minutes"] += aggregate.expected_minutes
            bucket["absence_minutes"] += aggregate.absence_minutes
            bucket["balance_minutes"] += aggregate.balance_minutes
            bucket["overtime_minutes"] += (
                aggregate.overtime_standard + aggregate.overtime_night
                + aggregate.overtime_weekend + aggregate.overtime_holiday
            )
            headcount.setdefault(key, set()).add(user.id)

    rows = []
    for key, bucket in sorted(buckets.items()):
        people = len(headcount.get(key, ()))
        bucket["headcount"] = people
        bucket["average_minutes"] = int(bucket["net_worked_minutes"] / people) if people else 0
        expected = bucket["expected_minutes"] or 1
        bucket["absence_rate"] = round(100 * bucket["absence_minutes"] / expected, 1)
        bucket["overtime_rate"] = round(100 * bucket["overtime_minutes"] / expected, 1)
        rows.append(bucket)

    return {
        "type": "summary",
        "title": f"Summary report by {group_by}",
        "columns": [
            {"key": "group", "label": group_by.title()},
            {"key": "headcount", "label": "Headcount"},
            {"key": "net_worked_minutes", "label": "Net worked", "type": "duration"},
            {"key": "expected_minutes", "label": "Expected", "type": "duration"},
            {"key": "balance_minutes", "label": "Balance", "type": "duration"},
            {"key": "average_minutes", "label": "Average per employee", "type": "duration"},
            {"key": "absence_minutes", "label": "Absence", "type": "duration"},
            {"key": "absence_rate", "label": "Absence rate %"},
            {"key": "overtime_minutes", "label": "Overtime", "type": "duration"},
            {"key": "overtime_rate", "label": "Overtime rate %"},
        ],
        "rows": rows,
        "totals": _sum(rows, ["net_worked_minutes", "expected_minutes", "balance_minutes",
                              "absence_minutes", "overtime_minutes"]),
        "meta": {"start": filters.start, "end": filters.end, "group_by": group_by},
    }


# ---------------------------------------------------------------------------
# FR-I-03 Weekly report
# ---------------------------------------------------------------------------


def weekly_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    week_start, _ = T.iso_week_bounds(filters.start, org.week_start)
    _, week_end = T.iso_week_bounds(filters.end, org.week_start)
    days = list(T.daterange(week_start, week_end))
    rows = []
    for user in users:
        aggregates = {
            a.day: a
            for a in db.scalars(
                select(DayAggregate).where(
                    DayAggregate.user_id == user.id,
                    DayAggregate.day >= week_start,
                    DayAggregate.day <= week_end,
                )
            ).all()
        }
        row = {
            "user_id": user.id,
            "personnel_number": user.personnel_number,
            "employee": user.display_name,
        }
        total = expected = 0
        for day in days:
            aggregate = aggregates.get(day)
            minutes = aggregate.net_worked_minutes if aggregate else 0
            row[day.isoformat()] = minutes
            total += minutes
            expected += aggregate.expected_minutes if aggregate else 0
        row["total_minutes"] = total
        row["expected_minutes"] = expected
        row["balance_minutes"] = total - expected
        rows.append(row)

    columns = [
        {"key": "personnel_number", "label": "Personnel no."},
        {"key": "employee", "label": "Employee"},
    ]
    columns += [
        {"key": d.isoformat(), "label": d.strftime("%a %d/%m"), "type": "duration"}
        for d in days
    ]
    columns += [
        {"key": "total_minutes", "label": "Total", "type": "duration"},
        {"key": "expected_minutes", "label": "Expected", "type": "duration"},
        {"key": "balance_minutes", "label": "Balance", "type": "duration"},
    ]
    return {
        "type": "weekly",
        "title": "Weekly report",
        "columns": columns,
        "rows": rows,
        "totals": _sum(rows, [d.isoformat() for d in days]
                       + ["total_minutes", "expected_minutes", "balance_minutes"]),
        "meta": {"start": week_start, "end": week_end},
    }


# ---------------------------------------------------------------------------
# FR-I-04 Detailed report
# ---------------------------------------------------------------------------


def detailed_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = {u.id: u for u in resolve_population(db, principal, filters)}
    names = _lookup(db, org.id)
    window_start = T.local_day_bounds(filters.start, org.timezone)[0]
    window_end = T.local_day_bounds(filters.end, org.timezone)[1]
    rows = []

    sessions = db.scalars(
        select(AttendanceSession).where(
            AttendanceSession.org_id == org.id,
            AttendanceSession.start_at < window_end,
            AttendanceSession.start_at >= window_start - timedelta(days=2),
        ).order_by(AttendanceSession.start_at)
    ).all()
    for session in sessions:
        user = users.get(session.user_id)
        if user is None:
            continue
        tzname = calc.user_timezone(db, org, user)
        local_start = T.to_local(session.start_at, tzname)
        if not (filters.start <= local_start.date() <= filters.end):
            continue
        if filters.cost_centre_ids and session.cost_centre_id not in filters.cost_centre_ids:
            continue
        breaks = sorted(session.breaks, key=lambda b: b.start_at)
        break_minutes = sum(
            int(((b.end_at or b.start_at) - b.start_at).total_seconds() // 60) for b in breaks
        )
        gross = (
            int((session.end_at - session.start_at).total_seconds() // 60)
            if session.end_at else 0
        )
        rows.append(
            {
                "entry_id": session.id,
                "personnel_number": user.personnel_number,
                "employee": user.display_name,
                "date": local_start.date().isoformat(),
                "start": local_start.strftime("%H:%M"),
                "end": (
                    T.to_local(session.end_at, tzname).strftime("%H:%M")
                    if session.end_at else "— running —"
                ),
                "gross_minutes": gross,
                "break_minutes": break_minutes,
                "net_minutes": max(0, gross - sum(
                    int(((b.end_at or b.start_at) - b.start_at).total_seconds() // 60)
                    for b in breaks if not b.is_paid
                )),
                "cost_centre": names["cost_centres"].get(session.cost_centre_id, ""),
                "description": session.description,
                "source": session.source,
                "device": session.device_id or "",
                "ip": session.ip or "",
                "recorded_by": "other" if session.recorded_by_other else "self",
                "system_generated": session.system_generated,
                "version": session.version,
                "superseded": bool(session.superseded_by),
            }
        )

    for entry in db.scalars(
        select(TimeEntry).where(
            TimeEntry.org_id == org.id,
            TimeEntry.day >= filters.start,
            TimeEntry.day <= filters.end,
        )
    ).all():
        user = users.get(entry.user_id)
        if user is None:
            continue
        rows.append(
            {
                "entry_id": entry.id,
                "personnel_number": user.personnel_number,
                "employee": user.display_name,
                "date": entry.day.isoformat(),
                "start": "", "end": "",
                "gross_minutes": entry.duration_minutes,
                "break_minutes": 0,
                "net_minutes": entry.duration_minutes,
                "cost_centre": names["cost_centres"].get(entry.cost_centre_id, ""),
                "description": entry.description,
                "source": entry.source,
                "device": "", "ip": "", "recorded_by": "self",
                "system_generated": False,
                "version": entry.version,
                "superseded": bool(entry.superseded_by),
            }
        )

    rows.sort(key=lambda r: (r["date"], r["employee"], r["start"]))
    return {
        "type": "detailed",
        "title": "Detailed report",
        "columns": [
            {"key": "personnel_number", "label": "Personnel no."},
            {"key": "employee", "label": "Employee"},
            {"key": "date", "label": "Date"},
            {"key": "start", "label": "Start"},
            {"key": "end", "label": "End"},
            {"key": "gross_minutes", "label": "Gross", "type": "duration"},
            {"key": "break_minutes", "label": "Breaks", "type": "duration"},
            {"key": "net_minutes", "label": "Net", "type": "duration"},
            {"key": "cost_centre", "label": "Cost centre"},
            {"key": "description", "label": "Description"},
            {"key": "source", "label": "Channel"},
            {"key": "device", "label": "Device"},
            {"key": "ip", "label": "IP"},
            {"key": "recorded_by", "label": "Recorded by"},
            {"key": "version", "label": "Version"},
        ],
        "rows": rows,
        "totals": _sum(rows, ["gross_minutes", "break_minutes", "net_minutes"]),
        "meta": {"start": filters.start, "end": filters.end},
    }


# ---------------------------------------------------------------------------
# FR-I-05 Absence report
# ---------------------------------------------------------------------------


def absence_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    policies = db.scalars(
        select(AbsencePolicy).where(
            AbsencePolicy.org_id == org.id, AbsencePolicy.archived.is_(False)
        )
    ).all()
    rows = []
    for user in users:
        for policy in policies:
            balance = absence_service.balance_for(db, user, policy, filters.start.year)
            requests = db.scalars(
                select(AbsenceRequest).where(
                    AbsenceRequest.user_id == user.id,
                    AbsenceRequest.policy_id == policy.id,
                    AbsenceRequest.start_date <= filters.end,
                    AbsenceRequest.end_date >= filters.start,
                )
            ).all()
            in_window = sum(
                r.deducted_minutes for r in requests if r.status == "approved"
            )
            if not in_window and not balance["taken_minutes"] and not balance["planned_minutes"]:
                continue
            rows.append(
                {
                    "personnel_number": user.personnel_number,
                    "employee": user.display_name,
                    "policy": policy.name,
                    "paid": "yes" if policy.is_paid else "no",
                    "entitlement_minutes": balance["accrued_minutes"] + balance["carried_over_minutes"],
                    "taken_minutes": balance["taken_minutes"],
                    "planned_minutes": balance["planned_minutes"],
                    "pending_minutes": balance["pending_minutes"],
                    "remaining_minutes": balance["remaining_minutes"],
                    "in_period_minutes": in_window,
                    "requests": len(requests),
                }
            )
    return {
        "type": "absence",
        "title": "Absence report",
        "columns": [
            {"key": "personnel_number", "label": "Personnel no."},
            {"key": "employee", "label": "Employee"},
            {"key": "policy", "label": "Policy"},
            {"key": "paid", "label": "Paid"},
            {"key": "entitlement_minutes", "label": "Entitlement", "type": "duration"},
            {"key": "taken_minutes", "label": "Taken", "type": "duration"},
            {"key": "planned_minutes", "label": "Planned", "type": "duration"},
            {"key": "pending_minutes", "label": "Pending", "type": "duration"},
            {"key": "remaining_minutes", "label": "Remaining", "type": "duration"},
            {"key": "in_period_minutes", "label": "In period", "type": "duration"},
        ],
        "rows": rows,
        "totals": _sum(rows, ["entitlement_minutes", "taken_minutes", "planned_minutes",
                              "remaining_minutes", "in_period_minutes"]),
        "meta": {"start": filters.start, "end": filters.end},
    }


# ---------------------------------------------------------------------------
# FR-I-06 Overtime report
# ---------------------------------------------------------------------------


def overtime_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    rows = []
    for user in users:
        totals = calc.period_totals(db, user.id, filters.start, filters.end)
        total = totals["overtime_total"]
        if total == 0:
            continue
        bank = absence_service.time_bank_balance(db, user.id)
        rows.append(
            {
                "personnel_number": user.personnel_number,
                "employee": user.display_name,
                "overtime_standard": totals["overtime_standard"],
                "overtime_night": totals["overtime_night"],
                "overtime_weekend": totals["overtime_weekend"],
                "overtime_holiday": totals["overtime_holiday"],
                "overtime_total": total,
                "approved_minutes": totals["overtime_approved_minutes"],
                "unapproved_minutes": total - totals["overtime_approved_minutes"],
                "time_bank_minutes": bank,
            }
        )
    return {
        "type": "overtime",
        "title": "Overtime report",
        "columns": [
            {"key": "personnel_number", "label": "Personnel no."},
            {"key": "employee", "label": "Employee"},
            {"key": "overtime_standard", "label": "Standard", "type": "duration"},
            {"key": "overtime_night", "label": "Night", "type": "duration"},
            {"key": "overtime_weekend", "label": "Weekend", "type": "duration"},
            {"key": "overtime_holiday", "label": "Public holiday", "type": "duration"},
            {"key": "overtime_total", "label": "Total", "type": "duration"},
            {"key": "approved_minutes", "label": "Approved", "type": "duration"},
            {"key": "unapproved_minutes", "label": "Unapproved", "type": "duration"},
            {"key": "time_bank_minutes", "label": "Time bank", "type": "duration"},
        ],
        "rows": rows,
        "totals": _sum(rows, ["overtime_standard", "overtime_night", "overtime_weekend",
                              "overtime_holiday", "overtime_total", "approved_minutes",
                              "unapproved_minutes"]),
        "meta": {"start": filters.start, "end": filters.end},
    }


# ---------------------------------------------------------------------------
# FR-I-07 Compliance report — the evidence artefact for a labour inspection
# ---------------------------------------------------------------------------


COMPLIANCE_TYPES = (
    "BREAK_SHORTFALL", "MIN_REST", "WEEKLY_REST", "MAX_WEEKLY_AVERAGE",
    "NIGHT_WORK_LIMIT", "DAILY_MAX_EXCEEDED",
)

RULE_LABELS = {
    "BREAK_SHORTFALL": "WT-04 rest break",
    "MIN_REST": "WT-02 minimum daily rest",
    "WEEKLY_REST": "WT-03 minimum weekly rest",
    "MAX_WEEKLY_AVERAGE": "WT-01 maximum average weekly working time",
    "NIGHT_WORK_LIMIT": "WT-05 night work limit",
    "DAILY_MAX_EXCEEDED": "Daily maximum working time",
}


def compliance_report(db: Session, org: Organisation, principal, filters) -> dict:
    users = {u.id: u for u in resolve_population(db, principal, filters)}
    query = select(AttendanceException).where(
        AttendanceException.org_id == org.id,
        AttendanceException.day >= filters.start,
        AttendanceException.day <= filters.end,
        AttendanceException.type.in_(COMPLIANCE_TYPES),
    )
    if filters.status:
        query = query.where(AttendanceException.status == filters.status)
    rows = []
    for record in db.scalars(query.order_by(AttendanceException.day.desc())).all():
        user = users.get(record.user_id)
        if user is None:
            continue
        rows.append(
            {
                "rule": RULE_LABELS.get(record.type, record.type),
                "personnel_number": user.personnel_number,
                "employee": user.display_name,
                "date": record.day.isoformat(),
                "detail": record.detail,
                "severity": record.severity,
                "status": record.status,
                "parameters": ", ".join(f"{k}={v}" for k, v in (record.rule_params or {}).items()),
                "resolution": record.resolution_note,
                "resolved_at": record.resolved_at.isoformat() if record.resolved_at else "",
            }
        )
    return {
        "type": "compliance",
        "title": "Working-time compliance report",
        "columns": [
            {"key": "rule", "label": "Rule"},
            {"key": "personnel_number", "label": "Personnel no."},
            {"key": "employee", "label": "Employee"},
            {"key": "date", "label": "Date"},
            {"key": "detail", "label": "Detail"},
            {"key": "severity", "label": "Severity"},
            {"key": "status", "label": "Status"},
            {"key": "parameters", "label": "Parameters in force"},
            {"key": "resolution", "label": "Resolution"},
        ],
        "rows": rows,
        "totals": {"breaches": len(rows),
                   "open": sum(1 for r in rows if r["status"] == "open")},
        "meta": {"start": filters.start, "end": filters.end,
                 "note": "Exportable for any historic period (section 16)."},
    }


# ---------------------------------------------------------------------------
# FR-I-08 Live team board
# ---------------------------------------------------------------------------


def live_board(db: Session, org: Organisation, principal, filters) -> dict:
    users = resolve_population(db, principal, filters)
    today = date.today()
    rows = []
    for user in users:
        tzname = calc.user_timezone(db, org, user)
        session = db.scalar(
            select(AttendanceSession)
            .where(
                AttendanceSession.user_id == user.id,
                AttendanceSession.end_at.is_(None),
                AttendanceSession.superseded_by.is_(None),
            )
            .order_by(AttendanceSession.start_at.desc())
        )
        on_break = bool(session and any(b.end_at is None for b in session.breaks))
        expected, _holiday = calc.expected_minutes(db, user, today)
        absence = db.scalar(
            select(AbsenceRequest).where(
                AbsenceRequest.user_id == user.id,
                AbsenceRequest.status == "approved",
                AbsenceRequest.start_date <= today,
                AbsenceRequest.end_date >= today,
            )
        )
        if session:
            state = "on_break" if on_break else "in"
        elif absence:
            state = "absent"
        elif expected > 0:
            aggregate = db.scalar(
                select(DayAggregate).where(
                    DayAggregate.user_id == user.id, DayAggregate.day == today
                )
            )
            state = "finished" if aggregate and aggregate.present_minutes else "expected"
        else:
            state = "off"
        rows.append(
            {
                "user_id": user.id,
                "employee": user.display_name,
                "personnel_number": user.personnel_number,
                "team": _lookup(db, org.id)["teams"].get(user.team_id, ""),
                "location": _lookup(db, org.id)["locations"].get(user.location_id, ""),
                "status": state,
                "since": (
                    T.to_local(session.start_at, tzname).strftime("%H:%M") if session else ""
                ),
                "expected_minutes": expected,
            }
        )
    order = {"in": 0, "on_break": 1, "expected": 2, "finished": 3, "absent": 4, "off": 5}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["employee"]))
    return {
        "type": "live_board",
        "title": "Live team board",
        "columns": [
            {"key": "employee", "label": "Employee"},
            {"key": "team", "label": "Team"},
            {"key": "status", "label": "Status"},
            {"key": "since", "label": "Since"},
            {"key": "location", "label": "Location"},
        ],
        "rows": rows,
        "totals": {
            "in": sum(1 for r in rows if r["status"] == "in"),
            "on_break": sum(1 for r in rows if r["status"] == "on_break"),
            "expected": sum(1 for r in rows if r["status"] == "expected"),
            "absent": sum(1 for r in rows if r["status"] == "absent"),
            "finished": sum(1 for r in rows if r["status"] == "finished"),
        },
        "meta": {"as_of": T.utcnow().isoformat(), "date": today},
    }


# ---------------------------------------------------------------------------
# Exception queue
# ---------------------------------------------------------------------------


def exception_queue(db: Session, org: Organisation, principal, filters) -> dict:
    users = {u.id: u for u in resolve_population(db, principal, filters)}
    rows = []
    for record in db.scalars(
        select(AttendanceException).where(
            AttendanceException.org_id == org.id,
            AttendanceException.status == (filters.status or "open"),
            AttendanceException.day >= filters.start,
            AttendanceException.day <= filters.end,
        )
    ).all():
        user = users.get(record.user_id)
        if user is None:
            continue
        rows.append(
            {
                "id": record.id,
                "employee": user.display_name,
                "personnel_number": user.personnel_number,
                "date": record.day.isoformat(),
                "type": record.type,
                "detail": record.detail,
                "severity": record.severity,
                "blocking": record.blocking,
                "age_days": (date.today() - record.day).days,
            }
        )
    rows.sort(key=lambda r: (not r["blocking"], -r["age_days"]))
    return {
        "type": "exception_queue",
        "title": "Exception queue",
        "columns": [
            {"key": "employee", "label": "Employee"},
            {"key": "date", "label": "Date"},
            {"key": "type", "label": "Type"},
            {"key": "detail", "label": "Detail"},
            {"key": "severity", "label": "Severity"},
            {"key": "blocking", "label": "Blocking"},
            {"key": "age_days", "label": "Age (days)"},
        ],
        "rows": rows,
        "totals": {"open": len(rows),
                   "blocking": sum(1 for r in rows if r["blocking"])},
        "meta": {"start": filters.start, "end": filters.end},
    }


BUILDERS = {
    "attendance": attendance_report,
    "summary": summary_report,
    "weekly": weekly_report,
    "detailed": detailed_report,
    "absence": absence_report,
    "overtime": overtime_report,
    "compliance": compliance_report,
    "live_board": live_board,
    "exception_queue": exception_queue,
}


def build(db: Session, org: Organisation, principal, report_type: str, filters) -> dict:
    builder = BUILDERS.get(report_type)
    if builder is None:
        raise ValueError(f"Unknown report type '{report_type}'")
    return builder(db, org, principal, filters)
