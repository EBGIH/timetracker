#!/usr/bin/env python3
"""Populate a demonstration workspace.

Creates the organisation, sites, teams, policies and the five personas from
section 7 of the specification, then generates six weeks of plausible
attendance so every report has something to show.

    python seed_demo.py            # create (refuses if data exists)
    python seed_demo.py --reset    # drop the database file first
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.database import Base, SessionLocal, engine, init_db
from app.models import (
    AbsencePolicy,
    AbsenceRequest,
    AttendanceSession,
    BreakRecord,
    BreakType,
    CostCentre,
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
from app.security import hash_secret, lookup_hash
from app.services import batch, calc, holidays, timeutil as T

PASSWORD = "TimeKeeper2026!"
RNG = random.Random(20260819)


def add_credential(db, user_id: str, kind: str, secret: str) -> None:
    hashed, salt = hash_secret(secret)
    db.add(
        Credential(
            user_id=user_id, type=kind, hash=hashed, salt=salt,
            lookup=lookup_hash(secret) if kind == "qr" else None,
        )
    )


def make_user(db, org, **kwargs) -> User:
    pattern_minutes = kwargs.pop("pattern", [480, 480, 480, 480, 480, 0, 0])
    shift = kwargs.pop("shift", (None, None))
    user = User(id=new_id(), org_id=org.id, **kwargs)
    db.add(user)
    db.flush()
    db.add(
        WorkingPattern(
            id=new_id(), user_id=user.id, valid_from=user.employment_start,
            contracted_hours_per_week=sum(pattern_minutes) / 60,
            expected_minutes=pattern_minutes,
            shift_start=shift[0], shift_end=shift[1],
        )
    )
    if user.has_login:
        add_credential(db, user.id, "password", PASSWORD)
    return user


def main() -> int:
    if "--reset" in sys.argv:
        Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    try:
        if db.scalar(select(Organisation.id)):
            print("A workspace already exists. Re-run with --reset to rebuild.")
            return 1

        org = Organisation(
            id=new_id(),
            name="Nordvest Manufacturing s.r.o.",
            country="SK",
            timezone="Europe/Bratislava",
            period_type="monthly",
            submission_cutoff_days=2,
            duration_format="hm",
            rounding_minutes=5,
            rounding_direction="nearest",
            max_session_hours=12,
            auto_stop_runaway=True,
            auto_break_after_minutes=0,
            managers_may_launch_kiosk=False,
            retention_years=3,
        )
        db.add(org)
        db.flush()

        plant = Location(id=new_id(), org_id=org.id, name="Trnava plant",
                         address="Priemyselná 12, Trnava", timezone="Europe/Bratislava",
                         geo_lat=48.3774, geo_lng=17.5872, geo_radius_m=250)
        office = Location(id=new_id(), org_id=org.id, name="Bratislava office",
                          address="Mlynské Nivy 5, Bratislava",
                          timezone="Europe/Bratislava")
        db.add_all([plant, office])
        db.flush()

        company = Team(id=new_id(), org_id=org.id, name="Nordvest")
        db.add(company)
        db.flush()
        production = Team(id=new_id(), org_id=org.id, name="Production",
                          parent_team_id=company.id)
        line_a = Team(id=new_id(), org_id=org.id, name="Line A",
                      parent_team_id=production.id)
        back_office = Team(id=new_id(), org_id=org.id, name="Back office",
                           parent_team_id=company.id)
        db.add_all([production, line_a, back_office])
        db.flush()

        for code, name in [("PROD", "Production"), ("MAINT", "Maintenance"),
                           ("ADMIN", "Administration"), ("PROJ", "Projects")]:
            db.add(CostCentre(id=new_id(), org_id=org.id, code=code, name=name))

        unpaid_break = BreakType(id=new_id(), org_id=org.id, name="Lunch (unpaid)",
                                 is_paid=False, max_minutes=60)
        paid_break = BreakType(id=new_id(), org_id=org.id, name="Short rest (paid)",
                               is_paid=True, max_minutes=15)
        db.add_all([unpaid_break, paid_break])

        db.add(
            OvertimeRule(
                id=new_id(), org_id=org.id, name="Standard",
                daily_threshold_minutes=480, weekly_threshold_minutes=2400,
                requires_prior_approval=False, night_start="22:00", night_end="06:00",
                weekend_days=[5, 6], time_bank_enabled=True,
                time_bank_cap_minutes=6000,
            )
        )

        annual = AbsencePolicy(
            id=new_id(), org_id=org.id, name="Annual leave", code="AL", is_paid=True,
            accrual_method="annual", accrual_rate_days=25, carry_over_limit_days=5,
            allow_negative=False, notice_days=3, approver_chain=["manager"],
            min_team_coverage=2,
        )
        sick = AbsencePolicy(
            id=new_id(), org_id=org.id, name="Sick leave", code="SICK", is_paid=True,
            accrual_method="unlimited", accrual_rate_days=0, allow_negative=True,
            notice_days=0, requires_document=True, approver_chain=["hr"],
        )
        unpaid = AbsencePolicy(
            id=new_id(), org_id=org.id, name="Unpaid leave", code="UNP", is_paid=False,
            accrual_method="unlimited", accrual_rate_days=0, allow_negative=True,
            approver_chain=["manager", "hr"],
        )
        lieu = AbsencePolicy(
            id=new_id(), org_id=org.id, name="Time off in lieu", code="TOIL",
            is_paid=True, accrual_method="unlimited", allow_negative=False,
            approver_chain=["manager"], funded_from_time_bank=True,
        )
        db.add_all([annual, sick, unpaid, lieu])
        db.flush()

        today = date.today()
        for year in (today.year, today.year + 1):
            for day, name in holidays.for_year("SK", year):
                from app.models import Holiday

                db.add(Holiday(id=new_id(), org_id=org.id, day=day, name=name))

        start = date(today.year - 1, 9, 1)

        owner = make_user(
            db, org, personnel_number="1000", first_name="Zuzana", last_name="Hrušková",
            email="owner@nordvest.example", role="owner", team_id=company.id,
            location_id=office.id, employment_start=start,
        )
        jana = make_user(
            db, org, personnel_number="1001", first_name="Jana", last_name="Kováčová",
            email="hr@nordvest.example", role="hr", team_id=back_office.id,
            location_id=office.id, employment_start=start,
        )
        peter = make_user(
            db, org, personnel_number="1002", first_name="Peter", last_name="Baláž",
            email="payroll@nordvest.example", role="hr", team_id=back_office.id,
            location_id=office.id, employment_start=start,
        )
        tomas = make_user(
            db, org, personnel_number="1003", first_name="Tomáš", last_name="Novák",
            email="manager@nordvest.example", role="manager", team_id=production.id,
            location_id=plant.id, employment_start=start,
        )
        lucia = make_user(
            db, org, personnel_number="1004", first_name="Lucia", last_name="Danková",
            email="lucia@nordvest.example", role="employee", team_id=back_office.id,
            location_id=office.id, employment_start=start,
            pattern=[480, 480, 480, 480, 480, 0, 0],
        )
        marek = make_user(
            db, org, personnel_number="2001", first_name="Marek", last_name="Šimko",
            email=None, role="limited", has_login=False, team_id=line_a.id,
            location_id=plant.id, employment_start=start,
            pattern=[480, 480, 480, 480, 480, 0, 0], shift=("06:00", "14:00"),
        )
        add_credential(db, marek.id, "pin", "4917")

        shift_workers = [marek]
        pins = {"Marek Šimko": "4917"}
        for index, (first, last, pin) in enumerate(
            [("Ivan", "Horváth", "2648"), ("Katarína", "Bieliková", "7351"),
             ("Ondrej", "Mikuláš", "5820"), ("Veronika", "Sedláková", "9174")],
            start=2,
        ):
            worker = make_user(
                db, org, personnel_number=f"200{index}", first_name=first,
                last_name=last, email=None, role="limited", has_login=False,
                team_id=line_a.id, location_id=plant.id, employment_start=start,
                pattern=[480, 480, 480, 480, 480, 0, 0], shift=("06:00", "14:00"),
            )
            add_credential(db, worker.id, "pin", pin)
            shift_workers.append(worker)
            pins[f"{first} {last}"] = pin

        part_timer = make_user(
            db, org, personnel_number="1005", first_name="Erik", last_name="Tóth",
            email="erik@nordvest.example", role="employee", team_id=back_office.id,
            location_id=office.id, employment_start=start,
            pattern=[300, 300, 300, 300, 0, 0, 0],
        )

        company.manager_user_id = owner.id
        production.manager_user_id = tomas.id
        line_a.manager_user_id = tomas.id
        back_office.manager_user_id = jana.id
        db.flush()

        kiosk = Kiosk(
            id=new_id(), org_id=org.id, name="Trnava plant — main entrance",
            location_id=plant.id, launch_token="demo-kiosk-token",
            token_expires_at=T.utcnow() + timedelta(days=365),
            session_hours=24, auth_method="pin4", breaks_enabled=True,
            assignee_ids=[w.id for w in shift_workers],
        )
        db.add(kiosk)
        db.flush()

        # --- Attendance history -------------------------------------------
        tzname = org.timezone
        office_staff = [(owner, "timer"), (jana, "timer"), (peter, "timer"),
                        (tomas, "timer"), (lucia, "timer"), (part_timer, "manual")]
        history_start = today - timedelta(days=45)

        for day in T.daterange(history_start, today):
            if day.weekday() >= 5:
                continue
            if calc.holiday_for(db, org.id, None, day):
                continue

            for worker in shift_workers:
                if RNG.random() < 0.06:
                    continue  # absence, sickness or a day off
                start_local = datetime.combine(day, time(6, 0)) + timedelta(
                    minutes=RNG.randint(-8, 6)
                )
                length = timedelta(hours=8, minutes=RNG.randint(-15, 75))
                end_local = start_local + length
                session = AttendanceSession(
                    id=new_id(), org_id=org.id, user_id=worker.id,
                    start_at=T.to_utc(start_local, tzname),
                    end_at=T.to_utc(end_local, tzname),
                    raw_start_at=T.to_utc(start_local, tzname),
                    raw_end_at=T.to_utc(end_local, tzname),
                    source="kiosk", status="closed", location_id=plant.id,
                    created_by=worker.id,
                )
                db.add(session)
                db.flush()
                lunch_start = start_local + timedelta(hours=4)
                db.add(
                    BreakRecord(
                        id=new_id(), session_id=session.id,
                        break_type_id=unpaid_break.id,
                        start_at=T.to_utc(lunch_start, tzname),
                        end_at=T.to_utc(lunch_start + timedelta(minutes=30), tzname),
                        is_paid=False,
                    )
                )

            for worker, source in office_staff:
                if RNG.random() < 0.08:
                    continue
                expected, _ = calc.expected_minutes(db, worker, day)
                if expected <= 0:
                    continue
                start_local = datetime.combine(day, time(8, 30)) + timedelta(
                    minutes=RNG.randint(-25, 45)
                )
                length = timedelta(minutes=expected + RNG.randint(-20, 60) + 30)
                end_local = start_local + length
                session = AttendanceSession(
                    id=new_id(), org_id=org.id, user_id=worker.id,
                    start_at=T.to_utc(start_local, tzname),
                    end_at=T.to_utc(end_local, tzname),
                    source=source, status="closed", location_id=office.id,
                    created_by=worker.id, description="Regular duties",
                )
                db.add(session)
                db.flush()
                lunch_start = start_local + timedelta(hours=4, minutes=RNG.randint(0, 60))
                db.add(
                    BreakRecord(
                        id=new_id(), session_id=session.id,
                        break_type_id=unpaid_break.id,
                        start_at=T.to_utc(lunch_start, tzname),
                        end_at=T.to_utc(lunch_start + timedelta(minutes=30), tzname),
                        is_paid=False,
                    )
                )

        # A deliberate open session so the exception queue is not empty.
        stale_start = T.to_utc(
            datetime.combine(today - timedelta(days=2), time(7, 0)), tzname
        )
        db.add(
            AttendanceSession(
                id=new_id(), org_id=org.id, user_id=lucia.id, start_at=stale_start,
                source="timer", status="open", location_id=office.id,
                created_by=lucia.id, description="Forgot to stop the timer",
            )
        )

        # --- Absence --------------------------------------------------------
        def add_absence(user, policy, start_date, end_date, status="approved", note=""):
            minutes = 0
            for day in T.daterange(start_date, end_date):
                expected, _ = calc.expected_minutes(db, user, day)
                minutes += expected
            db.add(
                AbsenceRequest(
                    id=new_id(), org_id=org.id, user_id=user.id, policy_id=policy.id,
                    start_date=start_date, end_date=end_date, status=status,
                    deducted_minutes=minutes, reason=note, created_by=user.id,
                    decided_by=tomas.id if status == "approved" else None,
                    decided_at=T.utcnow() if status == "approved" else None,
                )
            )

        add_absence(lucia, annual, today - timedelta(days=30), today - timedelta(days=26),
                    note="Family holiday")
        add_absence(marek, sick, today - timedelta(days=12), today - timedelta(days=10),
                    note="Influenza")
        add_absence(tomas, annual, today + timedelta(days=14), today + timedelta(days=18),
                    note="Summer leave")
        add_absence(shift_workers[1], annual, today + timedelta(days=7),
                    today + timedelta(days=9), status="pending", note="Family event")

        db.commit()

        # --- Derive aggregates, exceptions and periods ----------------------
        print("Computing aggregates and evaluating rules …")
        for user in db.scalars(select(User).where(User.org_id == org.id)).all():
            calc.recompute_range(db, org, user, history_start, today)
        db.commit()
        batch.evaluate_org(db, org, lookback_days=(today - history_start).days,
                           today=today)
        db.commit()

        print("\nDemo workspace ready.\n")
        print(f"  Organisation : {org.name}")
        print(f"  Password for every login account: {PASSWORD}\n")
        print("  Sign in as:")
        print(f"    owner    {owner.email}")
        print(f"    HR       {jana.email}")
        print(f"    payroll  {peter.email}")
        print(f"    manager  {tomas.email}")
        print(f"    employee {lucia.email}")
        print("\n  Kiosk: http://localhost:8000/kiosk.html?token=demo-kiosk-token")
        print("  Kiosk PINs:")
        for name, pin in pins.items():
            print(f"    {name:22} {pin}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
