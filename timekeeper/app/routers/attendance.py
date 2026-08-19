"""Attendance capture — live timer, manual entry, breaks and the weekly grid
(Modules C and E)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AttendanceException,
    AttendanceSession,
    BreakRecord,
    BreakType,
    CostCentre,
    DayAggregate,
    Organisation,
    TimeEntry,
    User,
    new_id,
    utcnow,
)
from ..schemas import (
    BreakStart,
    BreakStop,
    ExceptionResolution,
    GridSave,
    SessionIn,
    SessionUpdate,
    TimerStart,
    TimerStop,
)
from ..security import (
    Principal,
    assert_may_edit,
    assert_may_view,
    get_principal,
    visible_user_ids,
)
from ..services import calc, notifications, periods, rules, timeutil as T, webhooks

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def org_of(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(404, "Organisation not found")
    return org


def apply_rounding(org: Organisation, moment: datetime) -> datetime:
    """FR-A-09 / BR-07: rounding applies at clock-in and clock-out only, never
    to computed totals, and in the same direction for both events."""
    return T.round_timestamp(moment, org.rounding_minutes or 0, org.rounding_direction)


def local_day(db: Session, org: Organisation, user: User, moment: datetime) -> date:
    return T.to_local(moment, calc.user_timezone(db, org, user)).date()


def running_session(db: Session, user_id: str) -> AttendanceSession | None:
    return db.scalar(
        select(AttendanceSession)
        .where(
            AttendanceSession.user_id == user_id,
            AttendanceSession.end_at.is_(None),
            AttendanceSession.superseded_by.is_(None),
        )
        .order_by(AttendanceSession.start_at.desc())
    )


def assert_no_overlap(
    db: Session, user_id: str, start: datetime, end: datetime | None, exclude_id: str | None = None
) -> None:
    """FR-C-08 / section 12.3: no two sessions for one employee may overlap."""
    horizon = end or datetime(2999, 1, 1)
    query = select(AttendanceSession).where(
        AttendanceSession.user_id == user_id,
        AttendanceSession.superseded_by.is_(None),
        AttendanceSession.start_at < horizon,
    )
    if exclude_id:
        query = query.where(AttendanceSession.id != exclude_id)
    for other in db.scalars(query).all():
        other_end = other.end_at or datetime(2999, 1, 1)
        if other.start_at < horizon and other_end > start:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "overlap",
                    "message": "This would overlap an existing attendance session.",
                    "conflict": {
                        "id": other.id,
                        "start_at": other.start_at.isoformat(),
                        "end_at": other.end_at.isoformat() if other.end_at else None,
                        "description": other.description,
                    },
                    "resolutions": [
                        "stop_running_first" if other.end_at is None else "adjust_times",
                        "edit_conflicting_entry",
                    ],
                },
            )


def channel_enabled(org: Organisation, source: str) -> None:
    """FR-A-07: capture channels can be switched off per workspace."""
    mapping = {
        "timer": org.channel_timer,
        "manual": org.channel_manual,
        "grid": org.channel_grid,
        "kiosk": org.channel_kiosk,
        "mobile": org.channel_mobile,
        "api": True,
    }
    if not mapping.get(source, True):
        raise HTTPException(400, f"The '{source}' capture channel is disabled")


def validate_mandatory(org: Organisation, cost_centre_id, note, description) -> None:
    """FR-A-08."""
    if org.require_cost_centre and not cost_centre_id:
        raise HTTPException(400, "A cost centre is mandatory on time entries")
    if org.require_note and not (note or description):
        raise HTTPException(400, "A note is mandatory on time entries")


def serialise_session(db: Session, session: AttendanceSession, org: Organisation) -> dict:
    breaks = [
        {
            "id": b.id,
            "break_type_id": b.break_type_id,
            "start_at": b.start_at,
            "end_at": b.end_at,
            "is_paid": b.is_paid,
            "automatic": b.automatic,
            "minutes": int(((b.end_at or utcnow()) - b.start_at).total_seconds() // 60),
        }
        for b in sorted(session.breaks, key=lambda b: b.start_at)
    ]
    end = session.end_at or utcnow()
    unpaid = sum(b["minutes"] for b in breaks if not b["is_paid"])
    gross = int((end - session.start_at).total_seconds() // 60)
    return {
        "id": session.id,
        "user_id": session.user_id,
        "start_at": session.start_at,
        "end_at": session.end_at,
        "running": session.end_at is None,
        "description": session.description,
        "note": session.note,
        "cost_centre_id": session.cost_centre_id,
        "source": session.source,
        "device_id": session.device_id,
        "recorded_by_other": session.recorded_by_other,
        "system_generated": session.system_generated,
        "confirmed": session.confirmed,
        "within_geofence": session.within_geofence,
        "version": session.version,
        "gross_minutes": gross,
        "net_minutes": max(0, gross - unpaid),
        "breaks": breaks,
    }


def refresh_after_change(db: Session, org: Organisation, user: User, *days: date) -> None:
    for day in sorted({d for d in days if d}):
        rules.refresh(db, org, user, day, day)


def resolve_target(
    db: Session, principal: Principal, user_id: str | None, editing: bool = True
) -> User:
    target_id = user_id or principal.id
    if editing:
        assert_may_edit(db, principal, target_id)
    else:
        assert_may_view(db, principal, target_id)
    user = db.get(User, target_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(404, "User not found")
    return user


# ---------------------------------------------------------------------------
# Tracker screen (FR-C-07)
# ---------------------------------------------------------------------------


@router.get("/tracker")
def tracker(
    on: date | None = None,
    days: int = 7,
    user_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user = resolve_target(db, principal, user_id, editing=False)
    on = on or local_day(db, org, user, utcnow())
    start = on - timedelta(days=max(0, days - 1))
    tzname = calc.user_timezone(db, org, user)
    window = (T.local_day_bounds(start, tzname)[0], T.local_day_bounds(on, tzname)[1])

    sessions = db.scalars(
        select(AttendanceSession)
        .where(
            AttendanceSession.user_id == user.id,
            AttendanceSession.superseded_by.is_(None),
            AttendanceSession.start_at < window[1],
            (AttendanceSession.end_at.is_(None)) | (AttendanceSession.end_at > window[0]),
        )
        .order_by(AttendanceSession.start_at.desc())
    ).all()

    grouped: dict[str, list[dict]] = {}
    for session in sessions:
        key = T.to_local(session.start_at, tzname).date().isoformat()
        grouped.setdefault(key, []).append(serialise_session(db, session, org))

    period = periods.ensure_period(db, org, on)
    approval = periods.ensure_approval(db, period, user.id)
    calc.recompute_range(db, org, user, start, on)

    aggregates = {
        a.day.isoformat(): {
            "expected": a.expected_minutes,
            "present": a.present_minutes,
            "net": a.net_worked_minutes,
            "break_paid": a.break_paid_minutes,
            "break_unpaid": a.break_unpaid_minutes,
            "absence": a.absence_minutes,
            "balance": a.balance_minutes,
            "overtime": (
                a.overtime_standard + a.overtime_night
                + a.overtime_weekend + a.overtime_holiday
            ),
            "is_holiday": a.is_holiday,
        }
        for a in db.scalars(
            select(DayAggregate).where(
                DayAggregate.user_id == user.id,
                DayAggregate.day >= start,
                DayAggregate.day <= on,
            )
        ).all()
    }
    open_exceptions = db.scalars(
        select(AttendanceException).where(
            AttendanceException.user_id == user.id,
            AttendanceException.status == "open",
        )
    ).all()
    db.commit()

    running = running_session(db, user.id)
    return {
        "user": {"id": user.id, "name": user.display_name},
        "timezone": tzname,
        "today": on,
        "running": serialise_session(db, running, org) if running else None,
        "days": [
            {
                "day": (on - timedelta(days=offset)).isoformat(),
                "sessions": grouped.get((on - timedelta(days=offset)).isoformat(), []),
                "totals": aggregates.get((on - timedelta(days=offset)).isoformat(), {}),
            }
            for offset in range(days)
        ],
        "period": {
            "id": period.id,
            "start_date": period.start_date,
            "end_date": period.end_date,
            "status": period.status,
            "cutoff_date": period.cutoff_date,
            "approval_status": approval.status,
            "totals": calc.period_totals(db, user.id, period.start_date, period.end_date),
        },
        "exceptions": [
            {
                "id": e.id, "day": e.day, "type": e.type, "severity": e.severity,
                "blocking": e.blocking, "detail": e.detail,
            }
            for e in open_exceptions
        ],
    }


# ---------------------------------------------------------------------------
# Timer (FR-C-01, FR-C-02)
# ---------------------------------------------------------------------------


@router.post("/start")
def start_timer(
    payload: TimerStart,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user = principal.user
    channel_enabled(org, payload.source)
    validate_mandatory(org, payload.cost_centre_id, payload.note, payload.description)

    existing = running_session(db, user.id)
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "timer_running",
                "message": "A timer is already running. Stop it before starting a new one.",
                "running": {
                    "id": existing.id,
                    "start_at": existing.start_at.isoformat(),
                    "description": existing.description,
                },
            },
        )

    raw = T.naive_utc(payload.start_at) or utcnow()
    start = apply_rounding(org, raw)
    day = local_day(db, org, user, start)
    periods.assert_editable(db, org, user.id, day, principal)
    assert_no_overlap(db, user.id, start, None)

    within_geofence = None
    if payload.geo_lat is not None and payload.geo_lng is not None and user.location_id:
        within_geofence = _geofence_check(db, user, payload.geo_lat, payload.geo_lng)

    session = AttendanceSession(
        id=new_id(),
        org_id=org.id,
        user_id=user.id,
        start_at=start,
        raw_start_at=raw,
        source=payload.source,
        device_id=payload.device_id,
        ip=request.client.host if request.client else None,
        location_id=user.location_id,
        within_geofence=within_geofence,
        status="open",
        note=payload.note,
        description=payload.description,
        cost_centre_id=payload.cost_centre_id,
        created_by=principal.id,
    )
    db.add(session)
    db.flush()
    audit.record_for(
        db, principal, request, action="attendance.clock_in",
        entity_type="attendance_session", entity_id=session.id, after=session,
    )
    webhooks.emit(db, org.id, "clock_in", {"user_id": user.id, "session_id": session.id,
                                           "start_at": start.isoformat()})
    refresh_after_change(db, org, user, day)
    db.commit()
    return serialise_session(db, session, org)


def _geofence_check(db: Session, user: User, lat: float, lng: float) -> bool | None:
    """DP-13: only the boolean result and the site identifier are retained —
    never a track, never raw coordinates."""
    from math import asin, cos, radians, sin, sqrt

    from ..models import Location

    location = db.get(Location, user.location_id) if user.location_id else None
    if not location or location.geo_lat is None or location.geo_radius_m is None:
        return None
    lat1, lon1, lat2, lon2 = map(radians, [location.geo_lat, location.geo_lng, lat, lng])
    haversine = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    metres = 2 * 6371000 * asin(sqrt(haversine))
    return metres <= location.geo_radius_m


@router.post("/stop")
def stop_timer(
    payload: TimerStop,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user = principal.user
    session = running_session(db, user.id)
    if session is None:
        raise HTTPException(400, "No timer is running")
    raw = T.naive_utc(payload.end_at) or utcnow()
    end = apply_rounding(org, raw)
    if end <= session.start_at:
        raise HTTPException(400, "The end time must be later than the start time")

    for brk in session.breaks:
        if brk.end_at is None:
            brk.end_at = end

    before = audit.snapshot(session)
    session.end_at = end
    session.raw_end_at = raw
    session.status = "closed"
    if payload.note:
        session.note = payload.note
    audit.record_for(
        db, principal, request, action="attendance.clock_out",
        entity_type="attendance_session", entity_id=session.id,
        before=before, after=session,
    )
    webhooks.emit(db, org.id, "clock_out", {"user_id": user.id, "session_id": session.id,
                                            "end_at": end.isoformat()})
    refresh_after_change(
        db, org, user,
        local_day(db, org, user, session.start_at), local_day(db, org, user, end),
    )
    db.commit()
    return serialise_session(db, session, org)


# ---------------------------------------------------------------------------
# Manual entry and editing (FR-C-03 .. FR-C-06, FR-C-13)
# ---------------------------------------------------------------------------


@router.post("/sessions", status_code=201)
def create_session(
    payload: SessionIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user = resolve_target(db, principal, payload.user_id)
    channel_enabled(org, payload.source)
    validate_mandatory(org, payload.cost_centre_id, payload.note, payload.description)

    start = apply_rounding(org, T.naive_utc(payload.start_at))
    end = apply_rounding(org, T.naive_utc(payload.end_at)) if payload.end_at else None
    if end and end <= start:
        raise HTTPException(400, "The end time must be later than the start time")
    day = local_day(db, org, user, start)
    periods.assert_editable(db, org, user.id, day, principal)
    assert_no_overlap(db, user.id, start, end)

    recorded_by_other = user.id != principal.id
    session = AttendanceSession(
        id=new_id(), org_id=org.id, user_id=user.id, start_at=start, end_at=end,
        raw_start_at=T.naive_utc(payload.start_at),
        raw_end_at=T.naive_utc(payload.end_at) if payload.end_at else None,
        source=payload.source, ip=request.client.host if request.client else None,
        location_id=user.location_id, status="closed" if end else "open",
        note=payload.note, description=payload.description,
        cost_centre_id=payload.cost_centre_id, created_by=principal.id,
        recorded_by_other=recorded_by_other,
    )
    db.add(session)
    db.flush()
    audit.record_for(
        db, principal, request, action="attendance.created",
        entity_type="attendance_session", entity_id=session.id, after=session,
        note=payload.reason,
    )
    if recorded_by_other:
        # FR-C-13 / US-04 AC-5: the employee is always told.
        notifications.notify(
            db, user.id, "entry_amended",
            "An attendance record was created for you",
            f"{principal.user.display_name} recorded attendance on {day.isoformat()}.",
            f"/#/tracker?date={day.isoformat()}",
        )
    refresh_after_change(db, org, user, day, local_day(db, org, user, end) if end else None)
    db.commit()
    return serialise_session(db, session, org)


@router.put("/sessions/{session_id}")
def update_session(
    session_id: str,
    payload: SessionUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    session = db.get(AttendanceSession, session_id)
    if session is None or session.org_id != principal.org_id or session.superseded_by:
        raise HTTPException(404, "Session not found")
    user = resolve_target(db, principal, session.user_id)
    old_day = local_day(db, org, user, session.start_at)
    periods.assert_editable(db, org, user.id, old_day, principal)

    data = payload.model_dump(exclude_unset=True)
    reason = data.pop("reason", "") or payload.reason
    confirm = data.pop("confirm", None)

    # US-03 AC-2: setting a missing end time requires a reason.
    if session.end_at is None and data.get("end_at") and not reason:
        raise HTTPException(400, "A reason is required when correcting a clock-out")

    before = audit.snapshot(session)
    if "start_at" in data and data["start_at"]:
        session.start_at = apply_rounding(org, T.naive_utc(data["start_at"]))
    if "end_at" in data:
        session.end_at = (
            apply_rounding(org, T.naive_utc(data["end_at"])) if data["end_at"] else None
        )
        session.status = "closed" if session.end_at else "open"
    for field in ("description", "note", "cost_centre_id"):
        if field in data and data[field] is not None:
            setattr(session, field, data[field])
    if confirm:
        session.confirmed = True

    if session.end_at and session.end_at <= session.start_at:
        raise HTTPException(400, "The end time must be later than the start time")
    for brk in session.breaks:
        if brk.start_at < session.start_at or (
            session.end_at and (brk.end_at or session.end_at) > session.end_at
        ):
            raise HTTPException(
                400, "A break must lie entirely within its session (section 12.3)"
            )
    assert_no_overlap(db, user.id, session.start_at, session.end_at, exclude_id=session.id)

    session.version += 1
    corrected_by = "employee" if principal.id == user.id else "approver"
    audit.record_for(
        db, principal, request, action="attendance.updated",
        entity_type="attendance_session", entity_id=session.id,
        before=before, after=session,
        note=f"corrected by {corrected_by}: {reason}" if reason else f"corrected by {corrected_by}",
    )
    if principal.id != user.id:
        notifications.notify(
            db, user.id, "entry_amended", "One of your entries was amended",
            f"{principal.user.display_name} amended your record of {old_day.isoformat()}.",
            f"/#/tracker?date={old_day.isoformat()}",
        )
    new_day = local_day(db, org, user, session.start_at)
    refresh_after_change(db, org, user, old_day, new_day,
                         local_day(db, org, user, session.end_at) if session.end_at else None)
    db.commit()
    return serialise_session(db, session, org)


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    session = db.get(AttendanceSession, session_id)
    if session is None or session.org_id != principal.org_id:
        raise HTTPException(404, "Session not found")
    user = resolve_target(db, principal, session.user_id)
    day = local_day(db, org, user, session.start_at)
    periods.assert_editable(db, org, user.id, day, principal)
    audit.record_for(
        db, principal, request, action="attendance.deleted",
        entity_type="attendance_session", entity_id=session.id, before=session,
    )
    db.delete(session)
    db.flush()
    refresh_after_change(db, org, user, day)
    db.commit()
    return {"status": "deleted"}


@router.post("/sessions/{session_id}/duplicate", status_code=201)
def duplicate_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-C-06."""
    org = org_of(principal, db)
    original = db.get(AttendanceSession, session_id)
    if original is None or original.org_id != principal.org_id:
        raise HTTPException(404, "Session not found")
    user = resolve_target(db, principal, original.user_id)
    if original.end_at is None:
        raise HTTPException(400, "A running session cannot be duplicated")
    length = original.end_at - original.start_at
    start = original.end_at
    while True:
        try:
            assert_no_overlap(db, user.id, start, start + length)
            break
        except HTTPException:
            start += length or timedelta(minutes=1)
            if start - original.end_at > timedelta(days=1):
                raise HTTPException(409, "No free slot found to duplicate into")
    day = local_day(db, org, user, start)
    periods.assert_editable(db, org, user.id, day, principal)
    copy = AttendanceSession(
        id=new_id(), org_id=org.id, user_id=user.id, start_at=start, end_at=start + length,
        source="manual", status="closed", note=original.note,
        description=original.description, cost_centre_id=original.cost_centre_id,
        created_by=principal.id, location_id=original.location_id,
    )
    db.add(copy)
    db.flush()
    audit.record_for(
        db, principal, request, action="attendance.duplicated",
        entity_type="attendance_session", entity_id=copy.id, after=copy,
        note=f"duplicated from {original.id}",
    )
    refresh_after_change(db, org, user, day)
    db.commit()
    return serialise_session(db, copy, org)


@router.post("/sessions/{session_id}/continue", status_code=201)
def continue_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """FR-C-05: a new entry copying description and attributes, started now."""
    org = org_of(principal, db)
    original = db.get(AttendanceSession, session_id)
    if original is None or original.user_id != principal.id:
        raise HTTPException(404, "Session not found")
    if running_session(db, principal.id):
        raise HTTPException(409, "Stop the running timer first")
    start = apply_rounding(org, utcnow())
    assert_no_overlap(db, principal.id, start, None)
    day = local_day(db, org, principal.user, start)
    periods.assert_editable(db, org, principal.id, day, principal)
    session = AttendanceSession(
        id=new_id(), org_id=org.id, user_id=principal.id, start_at=start,
        source="timer", status="open", description=original.description,
        cost_centre_id=original.cost_centre_id, created_by=principal.id,
        location_id=original.location_id,
    )
    db.add(session)
    db.flush()
    audit.record_for(
        db, principal, request, action="attendance.continued",
        entity_type="attendance_session", entity_id=session.id, after=session,
    )
    refresh_after_change(db, org, principal.user, day)
    db.commit()
    return serialise_session(db, session, org)


# ---------------------------------------------------------------------------
# Breaks (Module E)
# ---------------------------------------------------------------------------


@router.post("/breaks/start")
def start_break(
    payload: BreakStart,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    session = running_session(db, principal.id)
    if session is None:
        # FR-E-05
        raise HTTPException(400, "A break cannot start while no attendance session is open")
    if any(b.end_at is None for b in session.breaks):
        raise HTTPException(400, "A break is already running")
    is_paid = False
    if payload.break_type_id:
        break_type = db.get(BreakType, payload.break_type_id)
        if break_type is None or break_type.org_id != org.id:
            raise HTTPException(404, "Break type not found")
        is_paid = break_type.is_paid
    start = T.naive_utc(payload.start_at) or utcnow()
    if start < session.start_at:
        raise HTTPException(400, "A break cannot start before the session")
    record = BreakRecord(
        id=new_id(), session_id=session.id, break_type_id=payload.break_type_id,
        start_at=start, is_paid=is_paid,
    )
    db.add(record)
    audit.record_for(
        db, principal, request, action="break.started", entity_type="break_record",
        entity_id=record.id, after=record,
    )
    webhooks.emit(db, org.id, "break_start", {"user_id": principal.id, "session_id": session.id})
    db.commit()
    return {"id": record.id, "start_at": record.start_at, "is_paid": record.is_paid}


@router.post("/breaks/stop")
def stop_break(
    payload: BreakStop,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    session = running_session(db, principal.id)
    if session is None:
        raise HTTPException(400, "No attendance session is open")
    record = next((b for b in session.breaks if b.end_at is None), None)
    if record is None:
        raise HTTPException(400, "No break is running")
    end = T.naive_utc(payload.end_at) or utcnow()
    if end <= record.start_at:
        raise HTTPException(400, "The break end must be later than its start")
    before = audit.snapshot(record)
    record.end_at = end
    if record.break_type_id:
        break_type = db.get(BreakType, record.break_type_id)
        if break_type and break_type.max_minutes:
            minutes = (end - record.start_at).total_seconds() / 60
            if minutes > break_type.max_minutes:
                rules.raise_exception(
                    db, org.id, principal.id,
                    local_day(db, org, principal.user, record.start_at),
                    "BREAK_SHORTFALL",
                    f"Break of {int(minutes)} min exceeds the maximum "
                    f"{break_type.max_minutes} min for '{break_type.name}'.",
                )
    audit.record_for(
        db, principal, request, action="break.ended", entity_type="break_record",
        entity_id=record.id, before=before, after=record,
    )
    webhooks.emit(db, org.id, "break_end", {"user_id": principal.id, "session_id": session.id})
    refresh_after_change(db, org, principal.user, local_day(db, org, principal.user, record.start_at))
    db.commit()
    return {"id": record.id, "end_at": record.end_at}


# ---------------------------------------------------------------------------
# Weekly timesheet grid (FR-C-10)
# ---------------------------------------------------------------------------


@router.get("/grid")
def read_grid(
    week_of: date | None = None,
    user_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    user = resolve_target(db, principal, user_id, editing=False)
    week_of = week_of or date.today()
    start, end = T.iso_week_bounds(week_of, org.week_start)
    rows = db.scalars(
        select(TimeEntry).where(
            TimeEntry.user_id == user.id,
            TimeEntry.day >= start,
            TimeEntry.day <= end,
            TimeEntry.superseded_by.is_(None),
        )
    ).all()
    centres = {
        c.id: {"id": c.id, "code": c.code, "name": c.name}
        for c in db.scalars(select(CostCentre).where(CostCentre.org_id == org.id)).all()
    }
    matrix: dict[str, dict[str, int]] = {}
    for row in rows:
        key = row.cost_centre_id or "_none"
        matrix.setdefault(key, {})[row.day.isoformat()] = row.duration_minutes
    aggregates = {
        a.day.isoformat(): a.expected_minutes
        for a in db.scalars(
            select(DayAggregate).where(
                DayAggregate.user_id == user.id,
                DayAggregate.day >= start,
                DayAggregate.day <= end,
            )
        ).all()
    }
    return {
        "user_id": user.id,
        "week_start": start,
        "week_end": end,
        "days": [d.isoformat() for d in T.daterange(start, end)],
        "cost_centres": list(centres.values()),
        "rows": [
            {
                "cost_centre_id": None if key == "_none" else key,
                "cells": cells,
                "total": sum(cells.values()),
            }
            for key, cells in matrix.items()
        ],
        "column_totals": {
            d.isoformat(): sum(
                cells.get(d.isoformat(), 0) for cells in matrix.values()
            )
            for d in T.daterange(start, end)
        },
        "expected": aggregates,
        "grand_total": sum(sum(c.values()) for c in matrix.values()),
    }


@router.post("/grid")
def save_grid(
    payload: GridSave,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = org_of(principal, db)
    channel_enabled(org, "grid")
    user = resolve_target(db, principal, payload.user_id)
    touched: set[date] = set()
    for cell in payload.cells:
        periods.assert_editable(db, org, user.id, cell.day, principal)
        existing = db.scalar(
            select(TimeEntry).where(
                TimeEntry.user_id == user.id,
                TimeEntry.day == cell.day,
                TimeEntry.cost_centre_id == cell.cost_centre_id,
                TimeEntry.superseded_by.is_(None),
            )
        )
        if existing is None:
            if cell.minutes <= 0:
                continue
            entry = TimeEntry(
                id=new_id(), org_id=org.id, user_id=user.id, day=cell.day,
                cost_centre_id=cell.cost_centre_id, description=cell.description,
                duration_minutes=cell.minutes, source="grid", created_by=principal.id,
            )
            db.add(entry)
            audit.record_for(
                db, principal, request, action="time_entry.created",
                entity_type="time_entry", entity_id=entry.id, after=entry,
            )
        else:
            before = audit.snapshot(existing)
            if cell.minutes <= 0:
                audit.record_for(
                    db, principal, request, action="time_entry.deleted",
                    entity_type="time_entry", entity_id=existing.id, before=before,
                )
                db.delete(existing)
            else:
                existing.duration_minutes = cell.minutes
                existing.description = cell.description
                existing.version += 1
                audit.record_for(
                    db, principal, request, action="time_entry.updated",
                    entity_type="time_entry", entity_id=existing.id,
                    before=before, after=existing,
                )
        touched.add(cell.day)
    db.commit()
    return {"status": "ok", "days": sorted(d.isoformat() for d in touched)}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


@router.get("/exceptions")
def list_exceptions(
    start: date | None = None,
    end: date | None = None,
    status_filter: str = "open",
    scope: str = "self",
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(AttendanceException).where(
        AttendanceException.org_id == principal.org_id
    )
    if scope == "self":
        query = query.where(AttendanceException.user_id == principal.id)
    else:
        allowed = None
        if principal.can("view_all_attendance"):
            allowed = None
        elif principal.can("view_team_attendance"):
            allowed = visible_user_ids(db, principal, "view_team_attendance")
        else:
            allowed = {principal.id}
        if allowed is not None:
            query = query.where(AttendanceException.user_id.in_(allowed))
    if status_filter != "all":
        query = query.where(AttendanceException.status == status_filter)
    if start:
        query = query.where(AttendanceException.day >= start)
    if end:
        query = query.where(AttendanceException.day <= end)

    rows = db.scalars(
        query.order_by(
            AttendanceException.blocking.desc(), AttendanceException.day.desc()
        )
    ).all()
    names = {
        u.id: u.display_name
        for u in db.scalars(select(User).where(User.org_id == principal.org_id)).all()
    }
    return [
        {
            "id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id, ""),
            "day": r.day, "type": r.type, "severity": r.severity,
            "blocking": r.blocking, "detail": r.detail, "status": r.status,
            "resolution_note": r.resolution_note, "resolved_at": r.resolved_at,
            "age_days": (date.today() - r.day).days,
        }
        for r in rows
    ]


@router.post("/exceptions/{exception_id}/resolve")
def resolve_exception(
    exception_id: str,
    payload: ExceptionResolution,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """US-07 AC-4: a note and an actor are stored and the exception is retained
    in history rather than deleted."""
    record = db.get(AttendanceException, exception_id)
    if record is None or record.org_id != principal.org_id:
        raise HTTPException(404, "Exception not found")
    if record.user_id != principal.id:
        principal.require("view_team_attendance")
        assert_may_view(db, principal, record.user_id)
    before = audit.snapshot(record)
    record.status = "resolved"
    record.resolved_by = principal.id
    record.resolved_at = utcnow()
    record.resolution_note = payload.note
    audit.record_for(
        db, principal, request, action="exception.resolved",
        entity_type="attendance_exception", entity_id=record.id,
        before=before, after=record,
    )
    db.commit()
    return {"status": "resolved"}
