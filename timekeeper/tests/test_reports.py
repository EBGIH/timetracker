"""The reporting catalogue (section 17) and the exporters (FR-I-10)."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest

from app.models import AbsenceRequest, CostCentre, Holiday, Location, Team, TimeEntry, new_id
from app.schemas import ReportFilters
from app.services import calc, exports, reports, rules
from conftest import add_session, make_user

JUNE = date(2026, 6, 1)      # Monday
WEEK_END = JUNE + timedelta(days=6)


class Principal:
    """A stand-in principal so the report builders can be exercised directly."""

    def __init__(self, user, role=None):
        self.user = user
        self.id = user.id
        self.org_id = user.org_id
        self.role = role or user.role

    def can(self, capability):
        from app.security import capability_scope

        return capability_scope(self.role, capability) is not None

    def scope(self, capability):
        from app.security import capability_scope

        return capability_scope(self.role, capability)


@pytest.fixture
def populated(db, org):
    team = Team(id=new_id(), org_id=org.id, name="Line A")
    site = Location(id=new_id(), org_id=org.id, name="Plant", timezone=org.timezone)
    centre = CostCentre(id=new_id(), org_id=org.id, code="PROD", name="Production")
    db.add_all([team, site, centre])
    db.flush()

    admin = make_user(db, org, "A", "Ada", "Admin", role="admin")
    worker = make_user(db, org, "W1", "Wanda", "Worker", team_id=team.id,
                       location_id=site.id)
    other = make_user(db, org, "W2", "Walter", "Worker", team_id=team.id,
                      location_id=site.id)
    team.manager_user_id = admin.id
    db.flush()

    for offset in range(5):
        day = JUNE + timedelta(days=offset)
        add_session(db, org, worker, day, "08:00", "17:00",
                    breaks=[("12:00", "12:30", False)])
        add_session(db, org, other, day, "06:00", "14:00")
    db.add(TimeEntry(id=new_id(), org_id=org.id, user_id=worker.id, day=JUNE,
                     cost_centre_id=centre.id, duration_minutes=480,
                     description="Line work", source="grid", created_by=worker.id))
    db.flush()

    for user in (worker, other):
        calc.recompute_range(db, org, user, JUNE, WEEK_END)
        for day in (JUNE + timedelta(days=i) for i in range(7)):
            rules.evaluate_day(db, org, user, day)
    db.flush()
    return {"admin": Principal(admin), "worker": worker, "other": other,
            "team": team, "site": site, "centre": centre}


def filters(**kwargs):
    base = {"start": JUNE, "end": WEEK_END}
    base.update(kwargs)
    return ReportFilters(**base)


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------


def test_attendance_report_has_one_row_per_employee_per_day(db, org, populated):
    report = reports.attendance_report(db, org, populated["admin"], filters())
    assert report["type"] == "attendance"
    worker_rows = [r for r in report["rows"] if r["personnel_number"] == "W1"]
    assert len(worker_rows) == 7
    monday = next(r for r in worker_rows if r["date"] == JUNE.isoformat())
    assert monday["net_worked_minutes"] == 510
    assert monday["expected_minutes"] == 480
    assert monday["difference_minutes"] == 30
    assert monday["first_in"] == "08:00"
    assert monday["last_out"] == "17:00"
    assert report["totals"]["net_worked_minutes"] > 0


def test_attendance_report_flags_days_with_no_record(db, org, populated):
    """US-06 AC-1: days with neither attendance nor absence still appear, and
    are flagged."""
    absent = make_user(db, org, "W3", "Wilma", "Worker")
    calc.recompute_range(db, org, absent, JUNE, WEEK_END)
    report = reports.attendance_report(db, org, populated["admin"], filters())
    rows = [r for r in report["rows"] if r["personnel_number"] == "W3"]
    assert len(rows) == 7
    working_days = [r for r in rows if r["expected_minutes"] > 0]
    assert all(r["exceptions"] for r in working_days)


def test_only_exceptions_filter(db, org, populated):
    report = reports.attendance_report(db, org, populated["admin"],
                                       filters(only_exceptions=True))
    assert all(row["exceptions"] for row in report["rows"])


@pytest.mark.parametrize("group_by", ["employee", "team", "location", "date", "week"])
def test_summary_report_groupings(db, org, populated, group_by):
    report = reports.summary_report(db, org, populated["admin"],
                                    filters(group_by=group_by))
    assert report["rows"]
    assert all("headcount" in row for row in report["rows"])
    assert all(row["absence_rate"] >= 0 for row in report["rows"])


def test_weekly_report_is_a_matrix_of_employees_by_day(db, org, populated):
    report = reports.weekly_report(db, org, populated["admin"], filters())
    row = next(r for r in report["rows"] if r["personnel_number"] == "W1")
    assert row[JUNE.isoformat()] == 510
    assert row["total_minutes"] == 5 * 510
    assert row["balance_minutes"] == 5 * 30
    assert len(report["columns"]) == 2 + 7 + 3


def test_detailed_report_lists_sessions_and_grid_entries(db, org, populated):
    report = reports.detailed_report(db, org, populated["admin"], filters())
    sources = {row["source"] for row in report["rows"]}
    assert "timer" in sources
    assert "grid" in sources
    session_row = next(r for r in report["rows"] if r["source"] == "timer")
    for key in ("start", "end", "gross_minutes", "break_minutes", "net_minutes",
                "version", "recorded_by"):
        assert key in session_row


def test_detailed_report_can_be_filtered_by_cost_centre(db, org, populated):
    report = reports.detailed_report(
        db, org, populated["admin"], filters(cost_centre_ids=[populated["centre"].id]))
    assert all(r["source"] == "grid" or r["cost_centre"] for r in report["rows"])


def test_absence_report_shows_balances(db, org, populated, annual_policy):
    db.add(AbsenceRequest(
        id=new_id(), org_id=org.id, user_id=populated["worker"].id,
        policy_id=annual_policy.id, start_date=JUNE + timedelta(days=1),
        end_date=JUNE + timedelta(days=2), status="approved", deducted_minutes=960))
    db.flush()
    report = reports.absence_report(db, org, populated["admin"], filters())
    row = next(r for r in report["rows"] if r["personnel_number"] == "W1")
    assert row["policy"] == "Annual leave"
    assert row["in_period_minutes"] == 960


def test_overtime_report_splits_the_categories(db, org, populated):
    add_session(db, org, populated["worker"], JUNE + timedelta(days=5), "08:00", "12:00")
    db.add(Holiday(id=new_id(), org_id=org.id, day=JUNE + timedelta(days=3),
                   name="Test holiday"))
    db.flush()
    calc.recompute_range(db, org, populated["worker"], JUNE, WEEK_END)
    report = reports.overtime_report(db, org, populated["admin"], filters())
    row = next(r for r in report["rows"] if r["personnel_number"] == "W1")
    assert row["overtime_weekend"] == 240
    assert row["overtime_holiday"] == 510
    assert row["overtime_total"] == (row["overtime_standard"] + row["overtime_night"]
                                     + row["overtime_weekend"] + row["overtime_holiday"])


def test_compliance_report_is_the_evidence_artefact(db, org, populated):
    add_session(db, org, populated["other"], WEEK_END - timedelta(days=1), "06:00", "16:00")
    rules.evaluate_day(db, org, populated["other"], WEEK_END - timedelta(days=1))
    report = reports.compliance_report(db, org, populated["admin"], filters())
    assert report["rows"]
    assert all(row["rule"].startswith(("WT-", "Daily")) for row in report["rows"])
    assert report["totals"]["breaches"] == len(report["rows"])


def test_exception_queue_sorts_blocking_first(db, org, populated):
    report = reports.exception_queue(db, org, populated["admin"], filters())
    blocking = [row["blocking"] for row in report["rows"]]
    assert blocking == sorted(blocking, reverse=True)


def test_live_board_counts_states(db, org, populated):
    report = reports.live_board(db, org, populated["admin"],
                                filters(start=date.today(), end=date.today()))
    assert set(report["totals"]) == {"in", "on_break", "expected", "absent", "finished"}
    assert len(report["rows"]) >= 3


def test_population_is_narrowed_by_team_and_employee(db, org, populated):
    by_team = reports.attendance_report(
        db, org, populated["admin"], filters(team_ids=[populated["team"].id]))
    assert {r["personnel_number"] for r in by_team["rows"]} == {"W1", "W2"}

    by_user = reports.attendance_report(
        db, org, populated["admin"], filters(user_ids=[populated["worker"].id]))
    assert {r["personnel_number"] for r in by_user["rows"]} == {"W1"}


def test_an_employee_only_ever_sees_themselves(db, org, populated):
    principal = Principal(populated["worker"], role="employee")
    report = reports.attendance_report(db, org, principal, filters())
    assert {r["personnel_number"] for r in report["rows"]} == {"W1"}


def test_unknown_report_type_is_rejected(db, org, populated):
    with pytest.raises(ValueError):
        reports.build(db, org, populated["admin"], "not_a_report", filters())


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------


def test_csv_export_matches_the_report(db, org, populated):
    report = reports.attendance_report(db, org, populated["admin"], filters())
    payload = exports.to_csv(report, "hm")
    text = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == [c["label"] for c in report["columns"]]
    assert len(rows) == len(report["rows"]) + 2  # header + rows + totals
    assert rows[-1][0] == "TOTAL"


def test_csv_totals_use_the_configured_duration_format(db, org, populated):
    report = reports.attendance_report(db, org, populated["admin"], filters())
    total = report["totals"]["net_worked_minutes"]
    hm = exports.to_csv(report, "hm").decode("utf-8-sig")
    decimal = exports.to_csv(report, "decimal").decode("utf-8-sig")
    assert f"{total // 60}:{total % 60:02d}" in hm
    assert f"{total / 60:.2f}" in decimal


def test_xlsx_and_pdf_exports_are_produced(db, org, populated):
    report = reports.weekly_report(db, org, populated["admin"], filters())
    assert exports.to_xlsx(report, "hm")[:2] == b"PK"
    assert exports.to_pdf(report, "hm", org.name)[:4] == b"%PDF"


def test_pdf_handles_an_empty_report(db, org, populated):
    report = reports.compliance_report(
        db, org, populated["admin"],
        filters(start=date(2019, 1, 1), end=date(2019, 1, 2)))
    assert report["rows"] == []
    assert exports.to_pdf(report, "hm", org.name)[:4] == b"%PDF"


def test_unsupported_export_format_is_rejected(db, org, populated):
    report = reports.weekly_report(db, org, populated["admin"], filters())
    with pytest.raises(ValueError):
        exports.export(report, "docx")
