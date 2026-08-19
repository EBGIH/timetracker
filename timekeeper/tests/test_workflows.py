"""End-to-end coverage of the remaining functional requirements: audit and
retention (Module L), integrations (Module J), notifications (Module K), the
weekly grid, the approver chain and the time bank."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def utc(days_ago: int = 0, hour: int = 8) -> datetime:
    """See tests/test_user_stories.py: weekends are stepped over so the suite
    is deterministic whatever day it runs on."""
    base = datetime.now(timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0, tzinfo=None)
    moment = base - timedelta(days=days_ago)
    while days_ago and moment.weekday() >= 5:
        moment -= timedelta(days=1)
    return moment


def workday(days_from_today: int) -> date:
    """The nearest working day at or after the offset, so a test that needs
    expected hours is not silently run on a Saturday."""
    day = date.today() + timedelta(days=days_from_today)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def past_workday(days_ago: int) -> date:
    day = date.today() - timedelta(days=days_ago)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# Module L — audit and data governance
# ---------------------------------------------------------------------------


def test_fr_l01_every_mutation_is_audited(api_client):
    employee = api_client.login_as("employee")
    owner = api_client.login_as("owner")
    session = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(utc(1, 8)) + "Z", "end_at": iso(utc(1, 16)) + "Z",
    }).json()
    api_client.put(f"/api/attendance/sessions/{session['id']}", headers=employee,
                   json={"description": "Updated", "reason": "typo"})
    api_client.delete(f"/api/attendance/sessions/{session['id']}", headers=employee)

    audit = api_client.get(f"/api/audit?entity_id={session['id']}", headers=owner).json()
    actions = {row["action"] for row in audit}
    assert {"attendance.created", "attendance.updated", "attendance.deleted"} <= actions
    for row in audit:
        assert row["occurred_at"]
        assert row["actor"]


def test_fr_l02_credentials_are_never_written_to_the_audit_log(api_client):
    owner = api_client.login_as("owner")
    target = api_client.tk["users"]["limited"]
    issued = api_client.post(f"/api/users/{target}/pin?digits=4", headers=owner).json()
    audit = api_client.get("/api/audit?action=pin.issued", headers=owner).json()
    assert audit
    serialised = str(audit)
    assert issued["pin"] not in serialised


def test_fr_l02_no_endpoint_can_modify_the_audit_log(api_client):
    owner = api_client.login_as("owner")
    rows = api_client.get("/api/audit", headers=owner).json()
    assert rows
    # There is no write path at all: the router exposes only GET.
    assert api_client.post("/api/audit", headers=owner, json={}).status_code == 405
    assert api_client.delete(f"/api/audit/{rows[0]['id']}", headers=owner).status_code in (404, 405)


def test_fr_l03_audit_log_is_searchable_and_exportable(api_client):
    owner = api_client.login_as("owner")
    filtered = api_client.get("/api/audit?entity_type=user&action=login.",
                              headers=owner).json()
    assert all(row["entity_type"] == "user" for row in filtered)
    export = api_client.get("/api/audit/export", headers=owner)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert b"occurred_at" in export.content


def test_fr_l04_retention_purges_and_logs_the_deletion(api_client):
    """Records older than the retention window are removed, and the deletion
    event itself is written to the audit log."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import AttendanceSession, AuditRecord, Organisation, new_id
    from app.services import batch

    hr = api_client.login_as("hr")
    db = SessionLocal()
    try:
        org = db.scalar(select(Organisation))
        old = AttendanceSession(
            id=new_id(), org_id=org.id, user_id=api_client.tk["users"]["employee"],
            start_at=datetime(2015, 3, 2, 7, 0), end_at=datetime(2015, 3, 2, 15, 0),
            source="manual", status="closed", created_by="test",
        )
        db.add(old)
        db.commit()
        result = batch.enforce_retention(db, org)
        db.commit()
        assert result["sessions"] >= 1
        assert db.get(AttendanceSession, old.id) is None
        purge = db.scalar(select(AuditRecord).where(AuditRecord.action == "retention.purged"))
        assert purge is not None
        assert purge.after_json["cutoff"]
    finally:
        db.close()


def test_fr_l05_subject_access_export(api_client):
    employee = api_client.login_as("employee")
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(utc(1, 8)) + "Z", "end_at": iso(utc(1, 16)) + "Z",
    })
    export = api_client.get(
        f"/api/users/{api_client.tk['users']['employee']}/data-export",
        headers=employee).json()
    for key in ("subject", "attendance_sessions", "absence_requests",
                "day_aggregates", "exceptions", "absence_balances"):
        assert key in export
    assert export["attendance_sessions"]
    assert "hash" not in str(export["subject"]).lower() or "[redacted]" in str(export)


def test_an_employee_cannot_export_someone_else(api_client):
    employee = api_client.login_as("employee")
    other = api_client.tk["users"]["outsider"]
    assert api_client.get(f"/api/users/{other}/data-export",
                          headers=employee).status_code == 403


def test_dp05_privacy_notice_is_available_in_product(api_client):
    employee = api_client.login_as("employee")
    notice = api_client.get("/api/privacy/notice", headers=employee).json()
    assert notice["what_is_not_recorded"]
    assert "screenshot" in " ".join(notice["what_is_not_recorded"]).lower()
    assert notice["how_long"]
    assert notice["your_rights"]


# ---------------------------------------------------------------------------
# Module F — approver chain, retrospective absence, coverage
# ---------------------------------------------------------------------------


def test_fr_f01_two_stage_approver_chain(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")

    policy = api_client.post("/api/org/absence-policies", headers=hr, json={
        "name": "Unpaid leave", "code": "UNP", "is_paid": False,
        "accrual_method": "unlimited", "accrual_rate_days": 0,
        "allow_negative": True, "approver_chain": ["manager", "hr"],
    }).json()

    start = date.today() + timedelta(days=20)
    request = api_client.post("/api/absence/requests", headers=employee, json={
        "policy_id": policy["id"], "start_date": start.isoformat(),
        "end_date": start.isoformat(),
    }).json()

    first = api_client.post(f"/api/absence/requests/{request['id']}/approve",
                            headers=manager, json={"note": "OK from me"}).json()
    assert first["status"] == "pending"
    assert first["stage"] == 1

    second = api_client.post(f"/api/absence/requests/{request['id']}/approve",
                             headers=hr, json={"note": "HR agrees"}).json()
    assert second["status"] == "approved"


def test_you_cannot_approve_your_own_absence(api_client):
    manager = api_client.login_as("manager")
    start = workday(25)
    created = api_client.post("/api/absence/requests", headers=manager, json={
        "policy_id": api_client.tk["policy_id"], "start_date": start.isoformat(),
        "end_date": start.isoformat(),
    })
    assert created.status_code == 201, created.text
    request = created.json()
    response = api_client.post(f"/api/absence/requests/{request['id']}/approve",
                               headers=manager, json={"note": ""})
    assert response.status_code == 403


def test_fr_f08_hr_records_absence_retrospectively(api_client):
    hr = api_client.login_as("hr")
    day = date.today() - timedelta(days=3)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    created = api_client.post("/api/absence/requests", headers=hr, json={
        "user_id": api_client.tk["users"]["employee"],
        "policy_id": api_client.tk["policy_id"],
        "start_date": day.isoformat(), "end_date": day.isoformat(),
        "reason": "Called in sick",
    })
    assert created.status_code == 201
    assert created.json()["status"] == "approved"


def test_fr_f07_manual_balance_adjustment_requires_a_reason(api_client):
    hr = api_client.login_as("hr")
    owner = api_client.login_as("owner")
    body = {
        "user_id": api_client.tk["users"]["employee"],
        "policy_id": api_client.tk["policy_id"],
        "year": date.today().year, "minutes": 480,
    }
    assert api_client.post("/api/absence/balance-adjustment", headers=hr,
                           json={**body, "reason": ""}).status_code == 422

    adjusted = api_client.post("/api/absence/balance-adjustment", headers=hr,
                               json={**body, "reason": "Carried from the old system"})
    assert adjusted.status_code == 200
    assert adjusted.json()["adjustment_minutes"] == 480
    audit = api_client.get("/api/audit?action=absence.balance_adjusted",
                           headers=owner).json()
    assert audit and "old system" in audit[0]["note"]


def test_overlapping_absence_requests_are_refused(api_client):
    employee = api_client.login_as("employee")
    start = date.today() + timedelta(days=60)
    body = {
        "policy_id": api_client.tk["policy_id"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
    }
    assert api_client.post("/api/absence/requests", headers=employee,
                           json=body).status_code == 201
    clash = api_client.post("/api/absence/requests", headers=employee, json=body)
    assert clash.status_code == 400
    assert any("overlaps" in e for e in clash.json()["errors"])


# ---------------------------------------------------------------------------
# Module G — time bank
# ---------------------------------------------------------------------------


def test_fr_g05_approved_overtime_feeds_the_time_bank(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    api_client.put("/api/org/overtime-rule", headers=hr, json={
        "daily_threshold_minutes": None, "weekly_threshold_minutes": None,
        "requires_prior_approval": False, "night_start": "22:00", "night_end": "06:00",
        "weekend_days": [5, 6], "time_bank_enabled": True,
        "time_bank_cap_minutes": 6000, "time_bank_carry_over": True,
    })

    day = utc(1, 8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=10)) + "Z",
    })
    periods = api_client.get("/api/periods", headers=hr).json()
    period = next(p for p in periods
                  if p["start_date"] <= day.date().isoformat() <= p["end_date"])
    api_client.post("/api/approvals/approve", headers=hr, json={
        "period_id": period["id"], "user_ids": [api_client.tk["users"]["employee"]],
    })
    bank = api_client.get("/api/time-bank", headers=employee).json()
    assert bank["balance_minutes"] > 0
    assert bank["movements"][0]["kind"] == "accrual"


def test_fr_g04_overtime_approval_flow(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    api_client.put("/api/org/overtime-rule", headers=hr, json={
        "daily_threshold_minutes": None, "weekly_threshold_minutes": None,
        "requires_prior_approval": True, "night_start": "22:00", "night_end": "06:00",
        "weekend_days": [5, 6], "time_bank_enabled": False,
        "time_bank_cap_minutes": 0, "time_bank_carry_over": False,
    })
    day = utc(1, 8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=10)) + "Z",
    })
    report = api_client.post("/api/reports/run/overtime", headers=hr, json={
        "start": day.date().isoformat(), "end": day.date().isoformat(),
    }).json()
    row = next(r for r in report["rows"] if r["personnel_number"] == "4")
    assert row["unapproved_minutes"] > 0
    assert row["approved_minutes"] == 0

    requested = api_client.post("/api/overtime/requests", headers=employee, json={
        "day": day.date().isoformat(), "minutes": 120, "reason": "Line breakdown",
    }).json()
    api_client.post(f"/api/overtime/requests/{requested['id']}/decide",
                    headers=manager, json={"decision": "approved"})

    report = api_client.post("/api/reports/run/overtime", headers=hr, json={
        "start": day.date().isoformat(), "end": day.date().isoformat(),
    }).json()
    row = next(r for r in report["rows"] if r["personnel_number"] == "4")
    assert row["approved_minutes"] > 0


# ---------------------------------------------------------------------------
# Module C — weekly grid, rounding, geofence
# ---------------------------------------------------------------------------


def test_fr_c10_weekly_grid_round_trip(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    centre = api_client.post("/api/org/cost-centres", headers=hr,
                             json={"code": "PROD", "name": "Production"}).json()
    monday = date.today() - timedelta(days=date.today().weekday())
    cells = [
        {"day": (monday + timedelta(days=i)).isoformat(), "cost_centre_id": centre["id"],
         "minutes": 480, "description": "Line work"}
        for i in range(5)
    ]
    saved = api_client.post("/api/attendance/grid", headers=employee, json={"cells": cells})
    assert saved.status_code == 200

    grid = api_client.get(f"/api/attendance/grid?week_of={monday}", headers=employee).json()
    assert grid["grand_total"] == 2400
    assert len(grid["rows"]) == 1

    zeroed = [{**cell, "minutes": 0} for cell in cells[:1]]
    api_client.post("/api/attendance/grid", headers=employee, json={"cells": zeroed})
    grid = api_client.get(f"/api/attendance/grid?week_of={monday}", headers=employee).json()
    assert grid["grand_total"] == 1920


def test_fr_a09_rounding_is_applied_at_clock_in(api_client):
    hr = api_client.login_as("hr")
    owner = api_client.login_as("owner")
    api_client.put("/api/org", headers=owner,
                   json={"rounding_minutes": 15, "rounding_direction": "nearest"})
    employee = api_client.login_as("employee")
    moment = utc(1, 8).replace(minute=52)
    created = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(moment) + "Z",
        "end_at": iso(moment + timedelta(hours=8, minutes=16)) + "Z",
    }).json()
    assert created["start_at"].endswith("T08:45:00")
    assert created["end_at"].endswith("T17:15:00")


def test_dp13_geofence_stores_only_the_boolean_result(api_client):
    hr = api_client.login_as("hr")
    owner = api_client.login_as("owner")
    locations = api_client.get("/api/org/locations", headers=hr).json()
    api_client.put(f"/api/org/locations/{locations[0]['id']}", headers=owner, json={
        "name": "Plant", "address": "", "timezone": "Europe/Bratislava",
        "geo_lat": 48.3774, "geo_lng": 17.5872, "geo_radius_m": 250,
    })
    api_client.put(f"/api/users/{api_client.tk['users']['employee']}", headers=hr,
                   json={"location_id": locations[0]["id"]})

    employee = api_client.login_as("employee")
    started = api_client.post("/api/attendance/start", headers=employee, json={
        "geo_lat": 48.3775, "geo_lng": 17.5873,
    }).json()
    assert started["within_geofence"] is True
    assert "geo_lat" not in started and "geo_lng" not in started


def test_fr_a07_a_disabled_channel_is_refused(api_client):
    owner = api_client.login_as("owner")
    api_client.put("/api/org", headers=owner, json={"channel_manual": False})
    employee = api_client.login_as("employee")
    response = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(utc(1, 8)) + "Z", "end_at": iso(utc(1, 16)) + "Z",
    })
    assert response.status_code == 400
    assert "disabled" in response.json()["message"]


def test_fr_a08_mandatory_fields_are_enforced(api_client):
    owner = api_client.login_as("owner")
    api_client.put("/api/org", headers=owner, json={"require_cost_centre": True})
    employee = api_client.login_as("employee")
    response = api_client.post("/api/attendance/start", headers=employee, json={})
    assert response.status_code == 400
    assert "cost centre" in response.json()["message"]


def test_fr_e05_a_break_cannot_start_without_a_session(api_client):
    employee = api_client.login_as("employee")
    response = api_client.post("/api/attendance/breaks/start", headers=employee, json={})
    assert response.status_code == 400
    assert "no attendance session is open" in response.json()["message"].lower()


def test_break_lifecycle_affects_net_time(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    api_client.post("/api/attendance/start", headers=employee, json={})
    started = api_client.post("/api/attendance/breaks/start", headers=employee,
                              json={"break_type_id": api_client.tk["break_type_id"]})
    assert started.status_code == 200
    assert started.json()["is_paid"] is False
    second = api_client.post("/api/attendance/breaks/start", headers=employee, json={})
    assert second.status_code == 400
    assert api_client.post("/api/attendance/breaks/stop", headers=employee,
                           json={}).status_code == 200


# ---------------------------------------------------------------------------
# Module B — bulk import, patterns, invitations
# ---------------------------------------------------------------------------


def test_fr_b08_bulk_import_dry_run_then_commit(api_client):
    hr = api_client.login_as("hr")
    csv = (
        "personnel_number,first_name,last_name,email,role,team,location,"
        "employment_start,hours_per_week\n"
        "900,Nova,Starter,nova@example.com,employee,Line A,Plant,2026-01-05,40\n"
        "901,Bad,Row,,employee,,,not-a-date,40\n"
    )
    dry = api_client.post("/api/users/import", headers=hr,
                          json={"csv": csv, "dry_run": True}).json()
    assert dry["rows"] == 2
    assert dry["valid"] == 1
    assert dry["created"] == 0

    committed = api_client.post("/api/users/import", headers=hr,
                                json={"csv": csv, "dry_run": False}).json()
    assert committed["created"] == 1
    people = api_client.get("/api/users", headers=hr).json()
    assert any(p["personnel_number"] == "900" for p in people)


def test_fr_b05_retrospective_pattern_change_recalculates(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    day = datetime.combine(past_workday(3), datetime.min.time()).replace(hour=8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    })
    response = api_client.post(
        f"/api/users/{api_client.tk['users']['employee']}/patterns", headers=hr, json={
            "valid_from": (date.today() - timedelta(days=10)).isoformat(),
            "contracted_hours_per_week": 20.0,
            "expected_minutes": [240, 240, 240, 240, 240, 0, 0],
        })
    assert response.status_code == 201
    assert response.json()["recalculated_from"]

    report = api_client.post("/api/reports/run/attendance", headers=hr, json={
        "start": day.date().isoformat(), "end": day.date().isoformat(),
        "user_ids": [api_client.tk["users"]["employee"]],
    }).json()
    assert report["rows"][0]["expected_minutes"] == 240


def test_overlapping_patterns_are_refused(api_client):
    hr = api_client.login_as("hr")
    body = {
        "valid_from": "2019-01-01", "contracted_hours_per_week": 40.0,
        "expected_minutes": [480, 480, 480, 480, 480, 0, 0],
    }
    response = api_client.post(
        f"/api/users/{api_client.tk['users']['employee']}/patterns", headers=hr, json=body)
    assert response.status_code == 400


def test_fr_b02_a_limited_member_cannot_have_a_login(api_client):
    hr = api_client.login_as("hr")
    response = api_client.post("/api/users", headers=hr, json={
        "personnel_number": "910", "first_name": "Kiosk", "last_name": "Only",
        "email": "kiosk@example.com", "role": "limited",
        "employment_start": "2026-01-01", "has_login": True,
    })
    assert response.status_code == 400


def test_fr_b06_deactivation_preserves_history(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    day = utc(1, 8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    })
    api_client.post(f"/api/users/{api_client.tk['users']['employee']}/deactivate",
                    headers=hr)
    report = api_client.post("/api/reports/run/detailed", headers=hr, json={
        "start": day.date().isoformat(), "end": day.date().isoformat(),
    }).json()
    assert any(r["personnel_number"] == "4" for r in report["rows"])


def test_fr_b03_invitation_round_trip(api_client):
    hr = api_client.login_as("hr")
    created = api_client.post("/api/users", headers=hr, json={
        "personnel_number": "920", "first_name": "New", "last_name": "Joiner",
        "email": "joiner@example.com", "role": "employee",
        "employment_start": date.today().isoformat(), "has_login": True,
    }).json()
    invite = api_client.post(f"/api/users/{created['id']}/invite", headers=hr).json()
    token = invite["invite_url"].rsplit("/", 1)[-1]

    assert api_client.get(f"/api/auth/invitation/{token}").status_code == 200
    accepted = api_client.post(f"/api/auth/invitation/{token}",
                               json={"new_password": "BrandNewPass1!"})
    assert accepted.status_code == 200
    login = api_client.post("/api/auth/login",
                            json={"email": "joiner@example.com",
                                  "password": "BrandNewPass1!"})
    assert login.status_code == 200
    # A token can only be used once.
    assert api_client.get(f"/api/auth/invitation/{token}").status_code == 404


# ---------------------------------------------------------------------------
# Module I / J — saved reports, share links, calendar feed, public API
# ---------------------------------------------------------------------------


def test_fr_i09_and_i12_saved_report_and_share_link(api_client):
    hr = api_client.login_as("hr")
    saved = api_client.post("/api/reports/saved", headers=hr, json={
        "name": "Monthly attendance", "report_type": "attendance",
        "filters": {"start": (date.today() - timedelta(days=7)).isoformat(),
                    "end": date.today().isoformat()},
        "schedule_cron": "0 6 1 * *", "schedule_recipients": ["payroll@example.com"],
    }).json()
    listed = api_client.get("/api/reports/saved/list", headers=hr).json()
    assert any(row["id"] == saved["id"] for row in listed)

    shared = api_client.post(f"/api/reports/saved/{saved['id']}/share", headers=hr,
                             json={"expires_in_days": 3}).json()
    token = shared["share_url"].rsplit("/", 1)[-1]
    public = api_client.get(f"/api/shared/{token}")
    assert public.status_code == 200
    assert public.json()["shared"] is True

    api_client.post(f"/api/reports/saved/{saved['id']}/unshare", headers=hr)
    assert api_client.get(f"/api/shared/{token}").status_code == 404


def test_expired_share_link_is_refused(api_client):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import SavedReport

    hr = api_client.login_as("hr")
    saved = api_client.post("/api/reports/saved", headers=hr, json={
        "name": "Expiring", "report_type": "attendance",
        "filters": {"start": date.today().isoformat(), "end": date.today().isoformat()},
    }).json()
    shared = api_client.post(f"/api/reports/saved/{saved['id']}/share", headers=hr,
                             json={"expires_in_days": 1}).json()
    token = shared["share_url"].rsplit("/", 1)[-1]

    db = SessionLocal()
    try:
        record = db.scalar(select(SavedReport).where(SavedReport.id == saved["id"]))
        record.share_expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    assert api_client.get(f"/api/shared/{token}").status_code == 410


def test_fr_j07_calendar_feed(api_client):
    hr = api_client.login_as("hr")
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    start = date.today() + timedelta(days=15)
    request = api_client.post("/api/absence/requests", headers=employee, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": start.isoformat(), "end_date": (start + timedelta(days=1)).isoformat(),
    }).json()
    api_client.post(f"/api/absence/requests/{request['id']}/approve", headers=manager,
                    json={"note": ""})
    feed = api_client.get("/api/integrations/calendar.ics", headers=hr)
    assert feed.status_code == 200
    body = feed.text
    assert "BEGIN:VCALENDAR" in body and "BEGIN:VEVENT" in body
    assert "Eva Employee" in body


def test_fr_j06_webhooks_are_queued_with_a_signature(api_client):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import WebhookDelivery

    owner = api_client.login_as("owner")
    created = api_client.post("/api/integrations/webhooks", headers=owner, json={
        "url": "https://example.com/hook", "events": ["clock_in", "clock_out"],
    }).json()
    employee = api_client.login_as("employee")
    api_client.post("/api/attendance/start", headers=employee, json={})

    db = SessionLocal()
    try:
        delivery = db.scalar(select(WebhookDelivery).where(WebhookDelivery.event == "clock_in"))
        assert delivery is not None
        assert delivery.signature
        assert delivery.payload["data"]["user_id"]
    finally:
        db.close()

    deliveries = api_client.get(
        f"/api/integrations/webhooks/{created['id']}/deliveries", headers=owner).json()
    assert deliveries


def test_unknown_webhook_event_is_refused(api_client):
    owner = api_client.login_as("owner")
    response = api_client.post("/api/integrations/webhooks", headers=owner, json={
        "url": "https://example.com/hook", "events": ["not_an_event"],
    })
    assert response.status_code == 400


def test_scim_and_sso_descriptors(api_client):
    owner = api_client.login_as("owner")
    scim = api_client.get("/api/integrations/scim/v2/Users", headers=owner).json()
    assert scim["totalResults"] >= 6
    assert scim["Resources"][0]["schemas"]
    sso = api_client.get("/api/integrations/sso/metadata", headers=owner).json()
    assert sso["acs_url"].endswith("/sso/acs")


# ---------------------------------------------------------------------------
# Module K — notifications
# ---------------------------------------------------------------------------


def test_fr_k01_employee_is_told_when_someone_amends_their_entry(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    day = utc(1, 8)
    session = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    }).json()
    api_client.put(f"/api/attendance/sessions/{session['id']}", headers=manager,
                   json={"description": "Reclassified", "reason": "Cost centre wrong"})
    notes = api_client.get("/api/notifications", headers=employee).json()
    assert any(n["type"] == "entry_amended" for n in notes)


def test_fr_k03_a_mandatory_notification_cannot_be_switched_off(api_client):
    employee = api_client.login_as("employee")
    api_client.put("/api/auth/notification-prefs", headers=employee,
                   json={"prefs": {"entry_amended": False, "exception_raised": False}})
    manager = api_client.login_as("manager")
    day = utc(1, 8)
    session = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    }).json()
    api_client.put(f"/api/attendance/sessions/{session['id']}", headers=manager,
                   json={"description": "x", "reason": "y"})
    notes = api_client.get("/api/notifications", headers=employee).json()
    assert any(n["type"] == "entry_amended" for n in notes)


def test_fr_k04_notifications_link_rather_than_embed_data(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    day = utc(1, 8)
    session = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    }).json()
    api_client.put(f"/api/attendance/sessions/{session['id']}", headers=manager,
                   json={"description": "x", "reason": "y"})
    note = next(n for n in api_client.get("/api/notifications", headers=employee).json()
                if n["type"] == "entry_amended")
    assert note["link"]
    assert len(note["body"]) < 200


def test_notifications_can_be_marked_read(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    day = utc(1, 8)
    session = api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    }).json()
    api_client.put(f"/api/attendance/sessions/{session['id']}", headers=manager,
                   json={"description": "x", "reason": "y"})
    assert api_client.get("/api/notifications?unread_only=true", headers=employee).json()
    api_client.post("/api/notifications/read-all", headers=employee)
    assert api_client.get("/api/notifications?unread_only=true",
                          headers=employee).json() == []


# ---------------------------------------------------------------------------
# Module A — holidays, settings, rule parameters
# ---------------------------------------------------------------------------


def test_fr_a06_holiday_import(api_client):
    hr = api_client.login_as("hr")
    result = api_client.post("/api/org/holidays/import", headers=hr, json={
        "year": 2027, "country": "SK", "location_id": None,
    }).json()
    assert result["created"] >= 13
    holidays = api_client.get("/api/org/holidays?year=2027", headers=hr).json()
    days = {row["day"] for row in holidays}
    assert "2027-01-01" in days
    assert "2027-12-25" in days
    # Re-importing must not duplicate.
    again = api_client.post("/api/org/holidays/import", headers=hr, json={
        "year": 2027, "country": "SK", "location_id": None,
    }).json()
    assert again["created"] == 0


def test_rule_parameters_are_saved_as_a_version(api_client):
    hr = api_client.login_as("hr")
    owner = api_client.login_as("owner")
    response = api_client.put("/api/org/rule-params", headers=hr, json={
        "effective_from": date.today().isoformat(),
        "params": {"wt04_min_break_minutes": 45, "wt02_min_daily_rest_minutes": 720},
    })
    assert response.status_code == 200
    current = api_client.get("/api/org/rule-params", headers=hr).json()
    assert current["params"]["wt04_min_break_minutes"] == 45
    assert current["defaults"]["wt04_min_break_minutes"] == 30
    audit = api_client.get("/api/audit?action=rule_params.updated", headers=owner).json()
    assert audit


def test_a_team_cannot_be_its_own_ancestor(api_client):
    hr = api_client.login_as("hr")
    parent = api_client.post("/api/org/teams", headers=hr, json={"name": "Parent"}).json()
    child = api_client.post("/api/org/teams", headers=hr,
                            json={"name": "Child", "parent_team_id": parent["id"]}).json()
    response = api_client.put(f"/api/org/teams/{parent['id']}", headers=hr, json={
        "name": "Parent", "parent_team_id": child["id"],
    })
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Reporting catalogue and role-appropriate visibility
# ---------------------------------------------------------------------------


def test_report_catalogue_is_filtered_by_role(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    employee_types = {row["type"] for row in
                      api_client.get("/api/reports/catalogue", headers=employee).json()}
    manager_types = {row["type"] for row in
                     api_client.get("/api/reports/catalogue", headers=manager).json()}
    assert "live_board" not in employee_types
    assert "live_board" in manager_types
    assert "attendance" in employee_types


def test_an_employee_report_covers_only_themselves(api_client):
    employee = api_client.login_as("employee")
    hr = api_client.login_as("hr")
    day = utc(1, 8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    })
    report = api_client.post("/api/reports/run/attendance", headers=employee, json={
        "start": day.date().isoformat(), "end": day.date().isoformat(),
    }).json()
    assert {row["user_id"] for row in report["rows"]} == {api_client.tk["users"]["employee"]}


def test_report_date_range_is_bounded(api_client):
    hr = api_client.login_as("hr")
    response = api_client.post("/api/reports/run/attendance", headers=hr, json={
        "start": "2020-01-01", "end": "2026-12-31",
    })
    assert response.status_code == 400


def test_live_board_states(api_client):
    manager = api_client.login_as("manager")
    employee = api_client.login_as("employee")
    api_client.post("/api/attendance/start", headers=employee, json={})
    board = api_client.post("/api/reports/run/live_board", headers=manager, json={
        "start": date.today().isoformat(), "end": date.today().isoformat(),
    }).json()
    row = next(r for r in board["rows"]
               if r["user_id"] == api_client.tk["users"]["employee"])
    assert row["status"] == "in"
    api_client.post("/api/attendance/breaks/start", headers=employee,
                    json={"break_type_id": api_client.tk["break_type_id"]})
    board = api_client.post("/api/reports/run/live_board", headers=manager, json={
        "start": date.today().isoformat(), "end": date.today().isoformat(),
    }).json()
    row = next(r for r in board["rows"]
               if r["user_id"] == api_client.tk["users"]["employee"])
    assert row["status"] == "on_break"


# ---------------------------------------------------------------------------
# Authentication hardening
# ---------------------------------------------------------------------------


def test_repeated_failed_logins_lock_the_account(api_client):
    for _ in range(10):
        response = api_client.post("/api/auth/login", json={
            "email": "employee@example.test", "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["message"] == "Invalid credentials"
    locked = api_client.post("/api/auth/login", json={
        "email": "employee@example.test", "password": "TestPassword123!"})
    assert locked.status_code == 429


def test_mfa_is_mandatory_for_privileged_roles(api_client):
    owner = api_client.login_as("owner")
    assert api_client.post("/api/auth/mfa/disable", headers=owner).status_code == 400
    profile = api_client.get("/api/auth/me", headers=owner).json()
    assert profile["mfa_required"] is True


def test_mfa_enrolment_and_login(api_client):
    from app.services import totp

    employee = api_client.login_as("employee")
    enrol = api_client.post("/api/auth/mfa/enrol", headers=employee).json()
    assert api_client.post("/api/auth/mfa/confirm", headers=employee,
                           json={"code": "000000"}).status_code == 400
    code = totp._code_at(enrol["secret"], int(__import__("time").time() // 30))
    assert api_client.post("/api/auth/mfa/confirm", headers=employee,
                           json={"code": code}).status_code == 200

    without_code = api_client.post("/api/auth/login", json={
        "email": "employee@example.test", "password": "TestPassword123!"})
    assert without_code.status_code == 401
    with_code = api_client.post("/api/auth/login", json={
        "email": "employee@example.test", "password": "TestPassword123!",
        "mfa_code": totp._code_at(enrol["secret"], int(__import__("time").time() // 30))})
    assert with_code.status_code == 200


def test_security_headers_are_set(api_client):
    response = api_client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
