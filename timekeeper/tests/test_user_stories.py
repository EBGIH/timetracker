"""Acceptance criteria from specification section 11 (US-01 .. US-07),
exercised through the API exactly as a user would.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

TOKEN = "test-kiosk-token"


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def utc(days_ago: int = 0, hour: int = 8, minute: int = 0) -> datetime:
    """A UTC timestamp `days_ago` days back, stepped further back if that would
    land on a weekend — otherwise the suite would behave differently depending
    on the day it is run."""
    base = datetime.now(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None
    )
    moment = base - timedelta(days=days_ago)
    while days_ago and moment.weekday() >= 5:
        moment -= timedelta(days=1)
    return moment


# ===========================================================================
# US-01 — Clock in at the kiosk
# ===========================================================================


def test_us01_ac1_roster_shows_my_name_and_status(api_client):
    response = api_client.get(f"/api/kiosk/session?token={TOKEN}")
    assert response.status_code == 200
    data = response.json()
    names = {row["name"]: row["status"] for row in data["roster"]}
    assert "Marek Shift" in names
    assert names["Marek Shift"] == "out"


def test_us01_ac1_roster_exposes_nothing_beyond_name_and_status(api_client):
    """FR-D-10: no personal data beyond the display name and clock status."""
    row = api_client.get(f"/api/kiosk/session?token={TOKEN}").json()["roster"][0]
    assert set(row) == {"id", "name", "status", "since"}


def test_us01_ac2_correct_pin_clocks_in_with_a_confirmation(api_client):
    user_id = api_client.tk["users"]["limited"]
    response = api_client.post(f"/api/kiosk/event?token={TOKEN}", json={
        "user_id": user_id, "pin": "1234", "action": "clock_in",
        "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clocked_in"
    assert body["user_name"] == "Marek Shift"
    assert body["at"]


def test_us01_ac3_wrong_pin_gives_a_generic_error_then_locks_out(api_client):
    user_id = api_client.tk["users"]["limited"]
    for _ in range(5):
        response = api_client.post(f"/api/kiosk/event?token={TOKEN}", json={
            "user_id": user_id, "pin": "0000", "action": "clock_in",
            "idempotency_key": uuid.uuid4().hex,
        })
        assert response.status_code == 401
        # The message must not reveal whether the name or the PIN was wrong.
        assert response.json()["message"] == "Not recognised — please try again"

    locked = api_client.post(f"/api/kiosk/event?token={TOKEN}", json={
        "user_id": user_id, "pin": "1234", "action": "clock_in",
        "idempotency_key": uuid.uuid4().hex,
    })
    assert locked.status_code == 429

    owner = api_client.login_as("owner")
    audit = api_client.get("/api/audit?action=kiosk.locked_out", headers=owner).json()
    assert audit, "the lock-out must raise an alert in the audit log"


def test_us01_ac4_primary_action_reflects_current_state(api_client):
    user_id = api_client.tk["users"]["limited"]
    api_client.post(f"/api/kiosk/event?token={TOKEN}", json={
        "user_id": user_id, "pin": "1234", "action": "clock_in",
        "idempotency_key": uuid.uuid4().hex,
    })
    roster = api_client.get(f"/api/kiosk/session?token={TOKEN}").json()["roster"]
    assert next(r for r in roster if r["id"] == user_id)["status"] == "in"

    again = api_client.post(f"/api/kiosk/event?token={TOKEN}", json={
        "user_id": user_id, "pin": "1234", "action": "clock_in",
        "idempotency_key": uuid.uuid4().hex,
    })
    assert again.status_code == 409
    assert again.json()["error"] == "already_clocked_in"


def test_us01_ac5_offline_events_sync_with_their_original_timestamps(api_client):
    user_id = api_client.tk["users"]["limited"]
    start = utc(days_ago=1, hour=6)
    end = utc(days_ago=1, hour=14)
    response = api_client.post(f"/api/kiosk/sync?token={TOKEN}", json={"events": [
        {"user_id": user_id, "pin": "1234", "action": "clock_in",
         "idempotency_key": "off-1", "occurred_at": iso(start)},
        {"user_id": user_id, "pin": "1234", "action": "clock_out",
         "idempotency_key": "off-2", "occurred_at": iso(end)},
    ]})
    assert response.status_code == 200
    assert all(item["ok"] for item in response.json()["results"])

    hr = api_client.login_as("hr")
    entries = api_client.get(
        f"/api/v1/entries?start={start.date()}&end={end.date()}&user_id={user_id}",
        headers=hr).json()
    assert entries
    assert entries[0]["start_at"].startswith(start.strftime("%Y-%m-%dT%H"))
    assert entries[0]["source"] == "kiosk"


def test_us01_ac5_replayed_events_are_not_double_booked(api_client):
    user_id = api_client.tk["users"]["limited"]
    payload = {"events": [{
        "user_id": user_id, "pin": "1234", "action": "clock_in",
        "idempotency_key": "replay-1", "occurred_at": iso(utc(days_ago=2, hour=6)),
    }]}
    first = api_client.post(f"/api/kiosk/sync?token={TOKEN}", json=payload).json()
    second = api_client.post(f"/api/kiosk/sync?token={TOKEN}", json=payload).json()
    assert first["results"][0]["status"] == "clocked_in"
    assert second["results"][0]["status"] == "duplicate_ignored"


def test_kiosk_token_is_revocable(api_client):
    owner = api_client.login_as("owner")
    kiosks = api_client.get("/api/kiosks", headers=owner).json()
    kiosk_id = kiosks[0]["id"]
    api_client.post(f"/api/kiosks/{kiosk_id}/revoke", headers=owner)
    assert api_client.get(f"/api/kiosk/session?token={TOKEN}").status_code == 401


# ===========================================================================
# US-02 — Track time with a timer
# ===========================================================================


def test_us02_ac1_start_puts_the_entry_at_the_top_of_today(api_client):
    headers = api_client.login_as("employee")
    started = api_client.post("/api/attendance/start",
                              headers=headers, json={"description": "Report writing"})
    assert started.status_code == 200
    assert started.json()["running"] is True

    tracker = api_client.get("/api/attendance/tracker", headers=headers).json()
    assert tracker["running"]["description"] == "Report writing"
    assert tracker["days"][0]["sessions"][0]["running"] is True


def test_us02_ac2_the_timer_is_server_side_and_survives_a_new_client(api_client):
    headers = api_client.login_as("employee")
    api_client.post("/api/attendance/start", headers=headers, json={})
    fresh_headers = api_client.login_as("employee")  # a different device/session
    tracker = api_client.get("/api/attendance/tracker", headers=fresh_headers).json()
    assert tracker["running"] is not None


def test_us02_ac3_editing_the_start_time_recalculates_and_is_audited(api_client):
    headers = api_client.login_as("employee")
    session = api_client.post("/api/attendance/start", headers=headers, json={}).json()
    earlier = datetime.fromisoformat(session["start_at"]) - timedelta(hours=2)
    updated = api_client.put(f"/api/attendance/sessions/{session['id']}", headers=headers,
                             json={"start_at": earlier.isoformat() + "Z",
                                   "reason": "Started before I opened the laptop"})
    assert updated.status_code == 200
    assert updated.json()["gross_minutes"] >= 120

    owner = api_client.login_as("owner")
    audit = api_client.get(
        f"/api/audit?entity_id={session['id']}&action=attendance.updated",
        headers=owner).json()
    assert audit
    assert audit[0]["before"]["start_at"] != audit[0]["after"]["start_at"]


def test_us02_ac4_a_second_timer_is_refused_without_creating_an_overlap(api_client):
    headers = api_client.login_as("employee")
    api_client.post("/api/attendance/start", headers=headers, json={})
    second = api_client.post("/api/attendance/start", headers=headers, json={})
    assert second.status_code == 409
    assert second.json()["error"] == "timer_running"

    tracker = api_client.get("/api/attendance/tracker", headers=headers).json()
    running = [s for day in tracker["days"] for s in day["sessions"] if s["running"]]
    assert len(running) == 1


def test_manual_entry_cannot_overlap_an_existing_one(api_client):
    headers = api_client.login_as("employee")
    api_client.post("/api/attendance/sessions", headers=headers, json={
        "start_at": iso(utc(days_ago=3, hour=8)) + "Z",
        "end_at": iso(utc(days_ago=3, hour=16)) + "Z",
    })
    clash = api_client.post("/api/attendance/sessions", headers=headers, json={
        "start_at": iso(utc(days_ago=3, hour=15)) + "Z",
        "end_at": iso(utc(days_ago=3, hour=18)) + "Z",
    })
    assert clash.status_code == 409
    assert clash.json()["error"] == "overlap"
    assert "conflict" in clash.json()


def test_us02_ac5_a_runaway_timer_is_flagged_and_blocks_submission(api_client):
    headers = api_client.login_as("employee")
    api_client.post("/api/attendance/sessions", headers=headers, json={
        "start_at": iso(utc(days_ago=2, hour=6)) + "Z", "end_at": None,
    })
    owner = api_client.login_as("owner")
    api_client.post("/api/admin/run-batch", headers=owner)

    exceptions = api_client.get("/api/attendance/exceptions", headers=headers).json()
    types = {row["type"] for row in exceptions}
    assert "LONG_SESSION" in types or "MISSING_CLOCK_OUT" in types
    assert any(row["blocking"] for row in exceptions)


# ===========================================================================
# US-03 — Correct a forgotten clock-out
# ===========================================================================


def test_us03_ac1_a_missing_end_time_is_flagged(api_client):
    headers = api_client.login_as("employee")
    api_client.post("/api/attendance/sessions", headers=headers, json={
        "start_at": iso(utc(days_ago=1, hour=8)) + "Z", "end_at": None,
    })
    owner = api_client.login_as("owner")
    api_client.post("/api/admin/run-batch", headers=owner)
    exceptions = api_client.get("/api/attendance/exceptions", headers=headers).json()
    assert any(row["type"] == "MISSING_CLOCK_OUT" for row in exceptions)


def test_us03_ac2_setting_an_end_time_requires_a_reason(api_client):
    headers = api_client.login_as("employee")
    session = api_client.post("/api/attendance/sessions", headers=headers, json={
        "start_at": iso(utc(days_ago=1, hour=8)) + "Z", "end_at": None,
    }).json()

    without_reason = api_client.put(f"/api/attendance/sessions/{session['id']}",
                                    headers=headers,
                                    json={"end_at": iso(utc(days_ago=1, hour=16)) + "Z"})
    assert without_reason.status_code == 400

    with_reason = api_client.put(f"/api/attendance/sessions/{session['id']}",
                                 headers=headers,
                                 json={"end_at": iso(utc(days_ago=1, hour=16)) + "Z",
                                       "reason": "Forgot to clock out"})
    assert with_reason.status_code == 200

    owner = api_client.login_as("owner")
    audit = api_client.get(f"/api/audit?entity_id={session['id']}", headers=owner).json()
    updated = [row for row in audit if row["action"] == "attendance.updated"][0]
    assert updated["before"]["end_at"] is None
    assert "corrected by employee" in updated["note"]


def test_us03_ac3_a_locked_period_offers_a_correction_request(api_client):
    employee_headers = api_client.login_as("employee")
    hr = api_client.login_as("hr")

    day = utc(days_ago=1, hour=8)
    session = api_client.post("/api/attendance/sessions", headers=employee_headers, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    }).json()

    periods = api_client.get("/api/periods", headers=hr).json()
    period = next(p for p in periods
                  if p["start_date"] <= day.date().isoformat() <= p["end_date"])

    users = api_client.get("/api/users", headers=hr).json()
    for user in users:
        api_client.post("/api/approvals/exclude", headers=hr, json={
            "period_id": period["id"], "user_id": user["id"],
            "reason": "Excluded for this test",
        })
    assert api_client.post("/api/approvals/lock", headers=hr, json={
        "period_id": period["id"], "reason": "Payroll closed"}).status_code == 200

    blocked = api_client.put(f"/api/attendance/sessions/{session['id']}",
                             headers=employee_headers,
                             json={"description": "changed", "reason": "typo"})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "period_locked"

    correction = api_client.post("/api/corrections", headers=employee_headers, json={
        "entity_type": "attendance_session", "entity_id": session["id"],
        "proposed": {"description": "Corrected description"},
        "reason": "The description was wrong",
    })
    assert correction.status_code == 201

    applied = api_client.post(f"/api/corrections/{correction.json()['id']}/approve",
                              headers=hr, json={"note": "Agreed"})
    assert applied.status_code == 200
    new_id_ = applied.json()["new_record_id"]
    assert new_id_ != session["id"], "the original record must not be overwritten"

    entries = api_client.get(
        f"/api/v1/entries?start={day.date()}&end={day.date()}", headers=hr).json()
    original = next(e for e in entries if e["id"] == session["id"])
    assert original["superseded_by"] == new_id_


# ===========================================================================
# US-04 — Approve a team's timesheets
# ===========================================================================


def _prepare_period(api_client):
    """One clean working day for the employee, in the current period."""
    employee = api_client.login_as("employee")
    hr = api_client.login_as("hr")
    day = utc(days_ago=1, hour=8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=8)) + "Z",
    })
    periods = api_client.get("/api/periods", headers=hr).json()
    period = next(p for p in periods
                  if p["start_date"] <= day.date().isoformat() <= p["end_date"])
    return employee, hr, period, day


def _resolve_blocking(api_client, hr, period):
    """The employee did not work every day of the period, so BR-06 raises an
    unexplained-absence exception for the rest. HR clears them so the approval
    path itself can be exercised."""
    employee = api_client.login_as("employee")
    # Submitting evaluates the whole period, which is what surfaces them.
    api_client.post("/api/approvals/submit", headers=employee,
                    json={"period_id": period["id"]})
    exceptions = api_client.get(
        f"/api/attendance/exceptions?scope=team&status_filter=open"
        f"&start={period['start_date']}&end={period['end_date']}", headers=hr).json()
    for row in exceptions:
        if row["blocking"]:
            api_client.post(f"/api/attendance/exceptions/{row['id']}/resolve",
                            headers=hr, json={"note": "Not scheduled to work."})


def test_us04_ac1_queue_shows_one_row_per_member_with_the_figures(api_client):
    employee, hr, period, _ = _prepare_period(api_client)
    manager = api_client.login_as("manager")
    queue = api_client.get(f"/api/approvals/queue?period_id={period['id']}",
                           headers=manager).json()
    assert queue["rows"]
    row = queue["rows"][0]
    for key in ("status", "worked_minutes", "expected_minutes", "difference_minutes",
                "overtime_minutes", "exception_count"):
        assert key in row


def test_us04_ac2_blocking_exceptions_prevent_approval(api_client):
    employee, hr, period, day = _prepare_period(api_client)
    # Leave an open session on another day so a blocking exception exists.
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day - timedelta(days=1)) + "Z", "end_at": None,
    })
    owner = api_client.login_as("owner")
    api_client.post("/api/admin/run-batch", headers=owner)

    submitted = api_client.post("/api/approvals/submit", headers=employee,
                                json={"period_id": period["id"]})
    assert submitted.status_code == 409
    assert submitted.json()["error"] == "blocking_exceptions"

    manager = api_client.login_as("manager")
    result = api_client.post("/api/approvals/approve", headers=manager, json={
        "period_id": period["id"],
        "user_ids": [api_client.tk["users"]["employee"]],
    }).json()
    assert result["approved"] == []
    assert result["skipped"][0]["reason"] == "unresolved blocking exceptions"


def test_us04_ac3_bulk_approval_in_a_single_action(api_client):
    employee, hr, period, _ = _prepare_period(api_client)
    _resolve_blocking(api_client, hr, period)
    assert api_client.post("/api/approvals/submit", headers=employee,
                           json={"period_id": period["id"]}).status_code == 200
    manager = api_client.login_as("manager")
    result = api_client.post("/api/approvals/approve", headers=manager, json={
        "period_id": period["id"],
        "user_ids": [api_client.tk["users"]["employee"]],
    }).json()
    assert api_client.tk["users"]["employee"] in result["approved"]

    notifications = api_client.get("/api/notifications", headers=employee).json()
    assert any("approved" in row["title"].lower() for row in notifications)


def test_us04_ac4_rejection_needs_a_reason_and_reopens_the_period(api_client):
    employee, hr, period, _ = _prepare_period(api_client)
    _resolve_blocking(api_client, hr, period)
    api_client.post("/api/approvals/submit", headers=employee,
                    json={"period_id": period["id"]})
    manager = api_client.login_as("manager")

    no_reason = api_client.post("/api/approvals/reject", headers=manager, json={
        "period_id": period["id"], "user_ids": [api_client.tk["users"]["employee"]],
        "reason": "",
    })
    assert no_reason.status_code == 400

    rejected = api_client.post("/api/approvals/reject", headers=manager, json={
        "period_id": period["id"], "user_ids": [api_client.tk["users"]["employee"]],
        "reason": "Tuesday looks wrong, please check",
    })
    assert rejected.status_code == 200

    queue = api_client.get(f"/api/approvals/queue?period_id={period['id']}",
                           headers=manager).json()
    row = next(r for r in queue["rows"]
               if r["user_id"] == api_client.tk["users"]["employee"])
    assert row["status"] == "open"
    notifications = api_client.get("/api/notifications", headers=employee).json()
    assert any("Tuesday looks wrong" in row["body"] for row in notifications)


def test_us04_ac5_manager_amendments_are_attributed_and_notified(api_client):
    employee, hr, period, day = _prepare_period(api_client)
    manager = api_client.login_as("manager")
    entries = api_client.get(f"/api/v1/entries?start={day.date()}&end={day.date()}",
                             headers=hr).json()
    session_id = next(e["id"] for e in entries
                      if e["user_id"] == api_client.tk["users"]["employee"])

    amended = api_client.put(f"/api/attendance/sessions/{session_id}", headers=manager,
                             json={"description": "Amended by the manager",
                                   "reason": "Agreed with the employee"})
    assert amended.status_code == 200

    owner = api_client.login_as("owner")
    audit = api_client.get(f"/api/audit?entity_id={session_id}", headers=owner).json()
    assert any(row["actor"] == "Milan Manager" for row in audit)
    notifications = api_client.get("/api/notifications", headers=employee).json()
    assert any("amended" in row["title"].lower() for row in notifications)


# ===========================================================================
# US-05 — Request annual leave
# ===========================================================================


def test_us05_ac1_the_form_shows_the_current_balance(api_client):
    headers = api_client.login_as("employee")
    preview = api_client.post("/api/absence/preview", headers=headers, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": (date.today() + timedelta(days=30)).isoformat(),
        "end_date": (date.today() + timedelta(days=32)).isoformat(),
    }).json()
    balance = preview["balance"]
    for key in ("entitlement_minutes", "taken_minutes", "planned_minutes",
                "remaining_minutes"):
        assert key in balance


def test_us05_ac2_weekends_and_holidays_are_excluded(api_client):
    headers = api_client.login_as("employee")
    hr = api_client.login_as("hr")
    # Find the next Monday so the range covers exactly one weekend.
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 + 7)
    api_client.post("/api/org/holidays", headers=hr, json={
        "day": (monday + timedelta(days=2)).isoformat(), "name": "Test holiday",
        "location_id": None, "is_working_day_override": False,
    })
    preview = api_client.post("/api/absence/preview", headers=headers, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": monday.isoformat(),
        "end_date": (monday + timedelta(days=6)).isoformat(),
    }).json()
    # Seven calendar days, minus two weekend days, minus one public holiday.
    assert preview["deducted_minutes"] == 4 * 480


def test_us05_ac3_exceeding_the_balance_blocks_submission(api_client):
    headers = api_client.login_as("employee")
    start = date.today() + timedelta(days=30)
    response = api_client.post("/api/absence/requests", headers=headers, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=120)).isoformat(),
    })
    assert response.status_code == 400
    assert any("Insufficient balance" in e for e in response.json()["errors"])


def test_us05_ac4_approved_absence_suppresses_missing_attendance(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    owner = api_client.login_as("owner")

    day = date.today() - timedelta(days=7)
    while day.weekday() >= 5:
        day -= timedelta(days=1)

    created = api_client.post("/api/absence/requests", headers=employee, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": day.isoformat(), "end_date": day.isoformat(),
        "reason": "Retrospective leave",
    })
    assert created.status_code == 201
    approved = api_client.post(
        f"/api/absence/requests/{created.json()['id']}/approve",
        headers=manager, json={"note": "Fine"})
    assert approved.json()["status"] == "approved"

    api_client.post("/api/admin/run-batch", headers=owner)
    exceptions = api_client.get(
        f"/api/attendance/exceptions?start={day}&end={day}", headers=employee).json()
    assert not any(row["type"] == "UNEXPLAINED_ABSENCE" for row in exceptions)

    calendar = api_client.get(
        f"/api/absence/calendar?start={day}&end={day}", headers=manager).json()
    assert any(day.isoformat() in entry["days"] for entry in calendar["entries"])


def test_us05_ac5_cancelling_restores_the_balance(api_client):
    employee = api_client.login_as("employee")
    manager = api_client.login_as("manager")
    start = date.today() + timedelta(days=30)

    before = api_client.get("/api/absence/balances", headers=employee).json()[0]
    created = api_client.post("/api/absence/requests", headers=employee, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=4)).isoformat(),
    }).json()
    api_client.post(f"/api/absence/requests/{created['id']}/approve",
                    headers=manager, json={"note": ""})
    during = api_client.get("/api/absence/balances", headers=employee).json()[0]
    assert during["remaining_minutes"] < before["remaining_minutes"]

    api_client.post(f"/api/absence/requests/{created['id']}/cancel",
                    headers=employee, json={"note": "Plans changed"})
    after = api_client.get("/api/absence/balances", headers=employee).json()[0]
    assert after["remaining_minutes"] == before["remaining_minutes"]


def test_br08_balance_is_consumed_at_approval_not_at_request(api_client):
    employee = api_client.login_as("employee")
    start = date.today() + timedelta(days=45)
    before = api_client.get("/api/absence/balances", headers=employee).json()[0]
    api_client.post("/api/absence/requests", headers=employee, json={
        "policy_id": api_client.tk["policy_id"],
        "start_date": start.isoformat(), "end_date": (start + timedelta(days=2)).isoformat(),
    })
    after = api_client.get("/api/absence/balances", headers=employee).json()[0]
    assert after["remaining_minutes"] == before["remaining_minutes"]
    assert after["pending_minutes"] > 0


# ===========================================================================
# US-06 — Run the attendance report for payroll
# ===========================================================================


def _close_period(api_client):
    """The full period-close cycle of section 8.2: resolve, submit, approve,
    exclude the rest with a reason, then lock."""
    employee, hr, period, day = _prepare_period(api_client)
    _resolve_blocking(api_client, hr, period)
    api_client.post("/api/approvals/submit", headers=employee,
                    json={"period_id": period["id"]})
    api_client.post("/api/approvals/approve", headers=hr, json={
        "period_id": period["id"], "user_ids": [api_client.tk["users"]["employee"]],
    })
    for user in api_client.get("/api/users", headers=hr).json():
        if user["id"] == api_client.tk["users"]["employee"]:
            continue
        api_client.post("/api/approvals/exclude", headers=hr, json={
            "period_id": period["id"], "user_id": user["id"],
            "reason": "Not in scope for this test close",
        })
    locked = api_client.post("/api/approvals/lock", headers=hr,
                             json={"period_id": period["id"], "reason": "Payroll"})
    assert locked.status_code == 200, locked.text
    return hr, period, day


def test_us06_ac1_every_working_day_appears_even_with_no_record(api_client):
    hr, period, day = _close_period(api_client)
    report = api_client.post("/api/reports/run/attendance", headers=hr, json={
        "start": period["start_date"], "end": period["end_date"],
    }).json()
    employee_rows = [r for r in report["rows"]
                     if r["user_id"] == api_client.tk["users"]["employee"]]
    assert len(employee_rows) >= 20
    empty_days = [r for r in employee_rows
                  if r["present_minutes"] == 0 and r["expected_minutes"] > 0]
    assert empty_days, "days with neither attendance nor absence must be present"
    assert all(r["exceptions"] for r in empty_days), "and must be flagged"


def test_us06_ac2_export_totals_match_the_screen(api_client):
    hr, period, _ = _close_period(api_client)
    filters = {"start": period["start_date"], "end": period["end_date"]}
    report = api_client.post("/api/reports/run/attendance", headers=hr, json=filters).json()

    csv_response = api_client.post("/api/reports/run/attendance/export?fmt=csv",
                                   headers=hr, json=filters)
    assert csv_response.status_code == 200
    text = csv_response.content.decode("utf-8-sig")
    total_line = text.strip().splitlines()[-1]
    from app.services.timeutil import format_duration

    expected = format_duration(report["totals"]["net_worked_minutes"], "hm")
    assert expected in total_line

    for fmt, prefix in (("xlsx", b"PK"), ("pdf", b"%PDF")):
        binary = api_client.post(f"/api/reports/run/attendance/export?fmt={fmt}",
                                 headers=hr, json=filters)
        assert binary.status_code == 200
        assert binary.content.startswith(prefix)


def test_us06_ac3_reconciliation_lists_every_changed_employee(api_client):
    hr, period, day = _close_period(api_client)
    first = api_client.post("/api/payroll/exports", headers=hr,
                            json={"period_id": period["id"]}).json()
    assert first["reconciliation"]["changes"] == []

    api_client.post("/api/approvals/unlock", headers=hr,
                    json={"period_id": period["id"], "reason": "Late correction"})
    # The employee's period is approved and read-only to them, so HR makes the
    # late amendment — which is exactly the case the reconciliation exists for.
    added = api_client.post("/api/attendance/sessions", headers=hr, json={
        "user_id": api_client.tk["users"]["employee"],
        "start_at": iso(day + timedelta(hours=9)) + "Z",
        "end_at": iso(day + timedelta(hours=11)) + "Z",
        "reason": "Overtime reported late",
    })
    assert added.status_code == 201, added.text

    second = api_client.post("/api/payroll/exports", headers=hr, json={
        "period_id": period["id"], "confirm_unlocked": True}).json()
    changes = second["reconciliation"]["changes"]
    assert changes
    assert "previous" in list(changes[0]["fields"].values())[0]
    assert "current" in list(changes[0]["fields"].values())[0]


def test_us06_ac4_exporting_an_open_period_needs_confirmation(api_client):
    employee, hr, period, _ = _prepare_period(api_client)
    blocked = api_client.post("/api/payroll/exports", headers=hr,
                              json={"period_id": period["id"]})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "period_not_locked"

    confirmed = api_client.post("/api/payroll/exports", headers=hr, json={
        "period_id": period["id"], "confirm_unlocked": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["period_locked"] is False


def test_fr_j04_exports_are_logged_and_re_downloadable(api_client):
    hr, period, _ = _close_period(api_client)
    export = api_client.post("/api/payroll/exports", headers=hr,
                             json={"period_id": period["id"]}).json()
    download = api_client.get(f"/api/payroll/exports/{export['id']}/download", headers=hr)
    assert download.status_code == 200
    assert download.headers["X-Checksum-SHA256"] == export["checksum"]

    owner = api_client.login_as("owner")
    audit = api_client.get("/api/audit?action=payroll.", headers=owner).json()
    assert any(row["action"] == "payroll.exported" for row in audit)


# ===========================================================================
# US-07 — Detect a statutory compliance breach
# ===========================================================================


def test_us07_compliance_report_lists_breaches_with_their_parameters(api_client):
    employee = api_client.login_as("employee")
    hr = api_client.login_as("hr")
    day = utc(days_ago=1, hour=8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=9)) + "Z",
    })
    owner = api_client.login_as("owner")
    api_client.post("/api/admin/run-batch", headers=owner)

    report = api_client.post("/api/reports/run/compliance", headers=hr, json={
        "start": (day - timedelta(days=3)).date().isoformat(),
        "end": day.date().isoformat(),
    }).json()
    rows = [r for r in report["rows"] if "WT-04" in r["rule"]]
    assert rows
    assert rows[0]["parameters"], "the parameters in force must be evidenced"


def test_us07_ac4_resolution_stores_a_note_and_an_actor(api_client):
    employee = api_client.login_as("employee")
    hr = api_client.login_as("hr")
    day = utc(days_ago=1, hour=8)
    api_client.post("/api/attendance/sessions", headers=employee, json={
        "start_at": iso(day) + "Z", "end_at": iso(day + timedelta(hours=9)) + "Z",
    })
    owner = api_client.login_as("owner")
    api_client.post("/api/admin/run-batch", headers=owner)

    exceptions = api_client.get("/api/attendance/exceptions", headers=employee).json()
    target = next(r for r in exceptions if r["type"] == "BREAK_SHORTFALL")
    resolved = api_client.post(f"/api/attendance/exceptions/{target['id']}/resolve",
                               headers=hr, json={"note": "Break taken, not recorded."})
    assert resolved.status_code == 200

    history = api_client.get("/api/attendance/exceptions?status_filter=resolved",
                             headers=employee).json()
    kept = next(r for r in history if r["id"] == target["id"])
    assert kept["resolution_note"] == "Break taken, not recorded."
    assert kept["resolved_at"]
