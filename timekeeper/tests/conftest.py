"""Test fixtures: an isolated database and a small reference organisation."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("TK_ENABLE_SCHEDULER", "0")
os.environ.setdefault("TK_PBKDF2_ITERATIONS", "1000")  # keep the suite quick

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    AbsencePolicy,
    AttendanceSession,
    BreakRecord,
    BreakType,
    Credential,
    Kiosk,
    Location,
    Organisation,
    OvertimeRule,
    Team,
    User,
    WorkingPattern,
    new_id,
)
from app.security import hash_secret  # noqa: E402
from app.services import timeutil as T  # noqa: E402

TZ = "Europe/Bratislava"
PASSWORD = "TestPassword123!"


@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=True, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    organisation = Organisation(
        id=new_id(), name="Test s.r.o.", country="SK", timezone=TZ,
        period_type="monthly", duration_format="hm",
    )
    db.add(organisation)
    db.flush()
    db.add(
        OvertimeRule(
            id=new_id(), org_id=organisation.id, daily_threshold_minutes=None,
            weekly_threshold_minutes=None, night_start="22:00", night_end="06:00",
            weekend_days=[5, 6], is_default=True,
        )
    )
    db.flush()
    return organisation


@pytest.fixture
def employee(db, org):
    return make_user(db, org, "E1", "Test", "Employee", role="employee")


def make_user(db, org, number, first, last, role="employee", email=None,
              pattern=None, team_id=None, location_id=None,
              start=date(2020, 1, 1), has_login=True, password=PASSWORD):
    user = User(
        id=new_id(), org_id=org.id, personnel_number=number, first_name=first,
        last_name=last, email=email, role=role, team_id=team_id,
        location_id=location_id, employment_start=start, has_login=has_login,
    )
    db.add(user)
    db.flush()
    db.add(
        WorkingPattern(
            id=new_id(), user_id=user.id, valid_from=start,
            contracted_hours_per_week=40.0,
            expected_minutes=pattern or [480, 480, 480, 480, 480, 0, 0],
        )
    )
    if has_login and password:
        hashed, salt = hash_secret(password)
        db.add(Credential(user_id=user.id, type="password", hash=hashed, salt=salt))
    db.flush()
    return user


def add_session(db, org, user, day: date, start: str, end: str | None,
                breaks: list[tuple[str, str, bool]] | None = None,
                source="timer"):
    """Times are local wall-clock strings, "HH:MM"; an end earlier than the
    start rolls over to the next day."""
    start_local = datetime.combine(day, time(*map(int, start.split(":"))))
    end_local = None
    if end is not None:
        end_local = datetime.combine(day, time(*map(int, end.split(":"))))
        if end_local <= start_local:
            end_local += timedelta(days=1)
    record = AttendanceSession(
        id=new_id(), org_id=org.id, user_id=user.id,
        start_at=T.to_utc(start_local, TZ),
        end_at=T.to_utc(end_local, TZ) if end_local else None,
        source=source, status="closed" if end_local else "open",
        created_by=user.id,
    )
    db.add(record)
    db.flush()
    for break_start, break_end, is_paid in breaks or []:
        bs = datetime.combine(day, time(*map(int, break_start.split(":"))))
        be = datetime.combine(day, time(*map(int, break_end.split(":"))))
        if bs < start_local:
            bs += timedelta(days=1)
        if be <= bs:
            be += timedelta(days=1)
        db.add(
            BreakRecord(
                id=new_id(), session_id=record.id,
                start_at=T.to_utc(bs, TZ), end_at=T.to_utc(be, TZ), is_paid=is_paid,
            )
        )
    db.flush()
    return record


@pytest.fixture
def break_types(db, org):
    unpaid = BreakType(id=new_id(), org_id=org.id, name="Lunch", is_paid=False, max_minutes=60)
    paid = BreakType(id=new_id(), org_id=org.id, name="Rest", is_paid=True, max_minutes=15)
    db.add_all([unpaid, paid])
    db.flush()
    return {"unpaid": unpaid, "paid": paid}


@pytest.fixture
def annual_policy(db, org):
    policy = AbsencePolicy(
        id=new_id(), org_id=org.id, name="Annual leave", code="AL", is_paid=True,
        accrual_method="annual", accrual_rate_days=25, allow_negative=False,
        notice_days=0, approver_chain=["manager"],
    )
    db.add(policy)
    db.flush()
    return policy


# ---------------------------------------------------------------------------
# API-level fixture: a live app bound to a file database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def api_client():
    from fastapi.testclient import TestClient

    from app.database import SessionLocal, engine
    from app.main import app

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    organisation = Organisation(
        id=new_id(), name="API Test s.r.o.", country="SK", timezone=TZ,
        period_type="monthly",
    )
    session.add(organisation)
    session.flush()
    site = Location(id=new_id(), org_id=organisation.id, name="Plant", timezone=TZ)
    session.add(site)
    session.flush()
    team = Team(id=new_id(), org_id=organisation.id, name="Line A")
    session.add(team)
    session.flush()

    owner = make_user(session, organisation, "1", "Olivia", "Owner",
                      role="owner", email="owner@example.test")
    hr = make_user(session, organisation, "2", "Hana", "HR", role="hr",
                   email="hr@example.test")
    manager = make_user(session, organisation, "3", "Milan", "Manager",
                        role="manager", email="manager@example.test", team_id=team.id)
    worker = make_user(session, organisation, "4", "Eva", "Employee",
                       role="employee", email="employee@example.test", team_id=team.id)
    other = make_user(session, organisation, "5", "Otto", "Outsider",
                      role="employee", email="outsider@example.test")
    limited = make_user(session, organisation, "6", "Marek", "Shift",
                        role="limited", has_login=False, team_id=team.id,
                        location_id=site.id, password=None)
    pin_hash, pin_salt = hash_secret("1234")
    session.add(Credential(user_id=limited.id, type="pin", hash=pin_hash, salt=pin_salt))
    team.manager_user_id = manager.id

    session.add(
        OvertimeRule(id=new_id(), org_id=organisation.id, night_start="22:00",
                     night_end="06:00", weekend_days=[5, 6], is_default=True)
    )
    unpaid = BreakType(id=new_id(), org_id=organisation.id, name="Lunch", is_paid=False)
    session.add(unpaid)
    policy = AbsencePolicy(
        id=new_id(), org_id=organisation.id, name="Annual leave", code="AL",
        is_paid=True, accrual_method="annual", accrual_rate_days=25,
        approver_chain=["manager"],
    )
    session.add(policy)
    kiosk = Kiosk(
        id=new_id(), org_id=organisation.id, name="Gate", location_id=site.id,
        launch_token="test-kiosk-token", assignee_ids=[limited.id],
        auth_method="pin4", breaks_enabled=True, session_hours=24,
        token_expires_at=T.utcnow() + timedelta(days=1),
    )
    session.add(kiosk)
    session.commit()

    context = {
        "org_id": organisation.id,
        "team_id": team.id,
        "location_id": site.id,
        "policy_id": policy.id,
        "break_type_id": unpaid.id,
        "kiosk_token": "test-kiosk-token",
        "users": {
            "owner": owner.id, "hr": hr.id, "manager": manager.id,
            "employee": worker.id, "outsider": other.id, "limited": limited.id,
        },
        "emails": {
            "owner": "owner@example.test", "hr": "hr@example.test",
            "manager": "manager@example.test", "employee": "employee@example.test",
            "outsider": "outsider@example.test",
        },
    }
    session.close()

    with TestClient(app) as client:
        client.tk = context

        def login_as(role: str) -> dict:
            response = client.post("/api/auth/login", json={
                "email": context["emails"][role], "password": PASSWORD})
            assert response.status_code == 200, response.text
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        client.login_as = login_as
        yield client
