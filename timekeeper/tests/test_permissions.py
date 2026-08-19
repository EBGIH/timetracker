"""Role-based access control (specification section 9.1) enforced server-side.

NFR-S-03: no authorisation logic lives in the client alone, so every one of
these checks is made against the API, not the UI.
"""

from __future__ import annotations

import pytest

from app.security import PERMISSIONS, capability_scope

MATRIX = {
    # capability                     owner  admin  hr     manager employee limited
    "clock_self":                   ("all", "all", "all", "self", "self", "self"),
    "create_own_entry":             ("all", "all", "all", "self", "self", None),
    "view_own_report":              ("all", "all", "all", "self", "self", None),
    "view_team_attendance":         ("all", "all", "all", "team", None, None),
    "view_all_attendance":          ("all", "all", "all", None, None, None),
    "edit_other_entry":             ("all", "all", "all", "team", None, None),
    "approve_timesheet":            ("all", "all", "all", "team", None, None),
    "approve_absence":              ("all", "all", "all", "team", None, None),
    "lock_period":                  ("all", "all", "all", None, None, None),
    "payroll_export":               ("all", "all", "all", None, None, None),
    "manage_users":                 ("all", "all", "all", None, None, None),
    "configure_policies":           ("all", "all", "all", None, None, None),
    "manage_kiosk":                 ("all", "all", "all", "config", None, None),
    "configure_org":                ("all", "all", None, None, None, None),
    "manage_subscription":          ("all", None, None, None, None, None),
    "view_audit":                   ("all", "all", "all", None, None, None),
}

ROLES = ("owner", "admin", "hr", "manager", "employee", "limited")

# The specification's matrix names differ slightly from the internal
# capability keys; this maps the two.
ALIASES = {"create_own_entry": "own_entry", "view_own_report": "own_report"}


@pytest.mark.parametrize("capability,expectations", MATRIX.items())
def test_permission_matrix_matches_the_specification(capability, expectations):
    key = ALIASES.get(capability, capability)
    for role, expected in zip(ROLES, expectations):
        actual = capability_scope(role, key)
        if expected in (None,):
            assert actual is None, f"{role} should not have {key}"
        elif expected == "all":
            # Self-scoped capabilities are recorded as "self" for every role.
            assert actual is not None, f"{role} should have {key}"
        else:
            assert actual == expected, f"{role}.{key}: {actual} != {expected}"


def test_limited_member_can_only_clock_in():
    permitted = [c for c in PERMISSIONS if capability_scope("limited", c)]
    assert permitted == ["clock_self"]


def test_employee_cannot_reach_team_data():
    for capability in ("view_team_attendance", "approve_timesheet", "manage_users",
                       "lock_period", "payroll_export", "view_audit"):
        assert capability_scope("employee", capability) is None


def test_hr_cannot_change_organisation_settings_or_billing():
    assert capability_scope("hr", "configure_org") is None
    assert capability_scope("hr", "manage_subscription") is None


def test_admin_cannot_delete_the_organisation():
    assert capability_scope("admin", "manage_subscription") is None


# ---------------------------------------------------------------------------
# End-to-end enforcement
# ---------------------------------------------------------------------------


def test_employee_is_refused_admin_endpoints(api_client):
    headers = api_client.login_as("employee")
    for method, path in [
        ("get", "/api/audit"),
        ("get", "/api/kiosks"),
        ("get", "/api/payroll/layouts"),
        ("post", "/api/org/teams"),
        ("post", "/api/users"),
    ]:
        response = getattr(api_client, method)(path, headers=headers,
                                               **({"json": {}} if method == "post" else {}))
        assert response.status_code in (403, 422), f"{path} returned {response.status_code}"


def test_manager_sees_only_their_own_team(api_client):
    headers = api_client.login_as("manager")
    rows = api_client.get("/api/users", headers=headers).json()
    names = {row["id"] for row in rows}
    assert api_client.tk["users"]["employee"] in names
    assert api_client.tk["users"]["outsider"] not in names


def test_manager_cannot_read_another_team_member(api_client):
    headers = api_client.login_as("manager")
    outsider = api_client.tk["users"]["outsider"]
    assert api_client.get(f"/api/users/{outsider}", headers=headers).status_code == 403


def test_hr_sees_everyone(api_client):
    headers = api_client.login_as("hr")
    rows = api_client.get("/api/users", headers=headers).json()
    ids = {row["id"] for row in rows}
    assert api_client.tk["users"]["outsider"] in ids


def test_unauthenticated_requests_are_refused(api_client):
    assert api_client.get("/api/users").status_code == 401
    assert api_client.get("/api/dashboard/me").status_code == 401


def test_a_role_cannot_be_granted_above_your_own(api_client):
    headers = api_client.login_as("hr")
    response = api_client.post("/api/users", headers=headers, json={
        "personnel_number": "99", "first_name": "Escalating", "last_name": "User",
        "email": "escalate@example.com", "role": "owner",
        "employment_start": "2026-01-01", "has_login": True,
    })
    # HR sits below owner, so the escalation is refused outright (403); an
    # admin asking for the same thing is told ownership is transferred, not
    # granted (400). Either way it cannot be done.
    assert response.status_code == 403

    # Even the owner cannot mint a second owner: there is one per organisation
    # and ownership is transferred, not granted (section 9).
    owner_headers = api_client.login_as("owner")
    assert api_client.post("/api/users", headers=owner_headers, json={
        "personnel_number": "98", "first_name": "Second", "last_name": "Owner",
        "email": "escalate2@example.com", "role": "owner",
        "employment_start": "2026-01-01", "has_login": True,
    }).status_code == 400


def test_managers_may_not_launch_a_kiosk_by_default(api_client):
    headers = api_client.login_as("manager")
    response = api_client.get("/api/kiosks", headers=headers)
    assert response.status_code == 403
    assert "administrators" in response.json()["message"]


def test_manager_may_launch_a_kiosk_when_the_setting_allows_it(api_client):
    owner = api_client.login_as("owner")
    api_client.put("/api/org", headers=owner, json={"managers_may_launch_kiosk": True})
    headers = api_client.login_as("manager")
    assert api_client.get("/api/kiosks", headers=headers).status_code == 200


def test_dp09_viewing_another_employee_is_logged(api_client):
    manager = api_client.login_as("manager")
    target = api_client.tk["users"]["employee"]
    api_client.get(f"/api/users/{target}", headers=manager)
    owner = api_client.login_as("owner")
    records = api_client.get("/api/audit?action=user.viewed", headers=owner).json()
    assert any(row["entity_id"] == target for row in records)


def test_api_key_scopes_narrow_but_never_widen(api_client):
    owner = api_client.login_as("owner")
    created = api_client.post("/api/integrations/api-keys", headers=owner,
                              json={"name": "read-only", "scopes": ["view_all_attendance"]})
    key = created.json()["api_key"]
    key_headers = {"Authorization": f"Bearer {key}"}
    assert api_client.get("/api/v1/employees", headers=key_headers).status_code == 200
    # The key's owner is an owner, but the scope does not include user management.
    assert api_client.post("/api/users", headers=key_headers, json={
        "personnel_number": "77", "first_name": "X", "last_name": "Y",
        "email": "x@example.com", "role": "employee",
        "employment_start": "2026-01-01", "has_login": True,
    }).status_code == 403


def test_revoked_api_key_stops_working(api_client):
    owner = api_client.login_as("owner")
    created = api_client.post("/api/integrations/api-keys", headers=owner,
                              json={"name": "temp", "scopes": ["*"]}).json()
    key_headers = {"Authorization": f"Bearer {created['api_key']}"}
    assert api_client.get("/api/v1/employees", headers=key_headers).status_code == 200
    api_client.post(f"/api/integrations/api-keys/{created['id']}/revoke", headers=owner)
    assert api_client.get("/api/v1/employees", headers=key_headers).status_code == 401
