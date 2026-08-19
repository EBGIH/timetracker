"""Physical data model.

Implements the logical model of specification section 12. All timestamps are
stored in UTC (section 12.3); the originating time zone is recorded separately
where it matters for interpretation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Organisation and configuration (Module A)
# ---------------------------------------------------------------------------


class Organisation(Base, TimestampMixin):
    """FR-A-01: every record is scoped to a workspace/organisation."""

    __tablename__ = "organisation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), default="SK")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Bratislava")

    # FR-A-04 display + week configuration
    week_start: Mapped[int] = mapped_column(Integer, default=0)  # 0=Monday
    date_format: Mapped[str] = mapped_column(String(20), default="DD/MM/YYYY")
    time_format: Mapped[str] = mapped_column(String(5), default="24h")
    duration_format: Mapped[str] = mapped_column(String(10), default="hm")

    # FR-A-05 attendance period
    period_type: Mapped[str] = mapped_column(String(20), default="monthly")
    period_anchor: Mapped[date | None] = mapped_column(Date, nullable=True)
    submission_cutoff_days: Mapped[int] = mapped_column(Integer, default=2)

    # FR-A-07 capture channels
    channel_timer: Mapped[bool] = mapped_column(Boolean, default=True)
    channel_manual: Mapped[bool] = mapped_column(Boolean, default=True)
    channel_grid: Mapped[bool] = mapped_column(Boolean, default=True)
    channel_kiosk: Mapped[bool] = mapped_column(Boolean, default=True)
    channel_mobile: Mapped[bool] = mapped_column(Boolean, default=True)

    # FR-A-08 mandatory entry fields
    require_cost_centre: Mapped[bool] = mapped_column(Boolean, default=False)
    require_project: Mapped[bool] = mapped_column(Boolean, default=False)
    require_note: Mapped[bool] = mapped_column(Boolean, default=False)

    # FR-A-09 / BR-07 rounding
    rounding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    rounding_direction: Mapped[str] = mapped_column(String(10), default="nearest")

    # FR-C-09 runaway session handling
    max_session_hours: Mapped[int] = mapped_column(Integer, default=12)
    auto_stop_runaway: Mapped[bool] = mapped_column(Boolean, default=False)

    # FR-E-03 automatic break deduction
    auto_break_after_minutes: Mapped[int] = mapped_column(Integer, default=0)
    auto_break_minutes: Mapped[int] = mapped_column(Integer, default=0)

    # Section 9 footnote: may managers launch a kiosk?
    managers_may_launch_kiosk: Mapped[bool] = mapped_column(Boolean, default=False)

    # FR-H-08 auto approval
    auto_approve_after_days: Mapped[int] = mapped_column(Integer, default=0)

    # FR-L-04 / DP-08 retention
    retention_years: Mapped[int] = mapped_column(Integer, default=3)

    # Working-time rule parameters (section 16), versioned via RuleParamVersion
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)

    locations: Mapped[list["Location"]] = relationship(back_populates="organisation")


class Location(Base, TimestampMixin):
    """FR-A-03: sites with their own time zone and optional geofence."""

    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(400), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Bratislava")
    geo_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    geo_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    geo_radius_m: Mapped[int | None] = mapped_column(Integer, nullable=True)

    organisation: Mapped[Organisation] = relationship(back_populates="locations")


class Team(Base, TimestampMixin):
    """FR-A-02: hierarchy of at least three levels with a manager per node."""

    __tablename__ = "team"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    parent_team_id: Mapped[str | None] = mapped_column(
        ForeignKey("team.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))
    manager_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class UserGroup(Base, TimestampMixin):
    """FR-B-07: groups for bulk assignment of policies and permissions."""

    __tablename__ = "user_group"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(200))
    member_ids: Mapped[list] = mapped_column(JSON, default=list)


class CostCentre(Base, TimestampMixin):
    """Lightweight cost-centre attribute retained from the Clockify teardown."""

    __tablename__ = "cost_centre"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Holiday(Base, TimestampMixin):
    """FR-A-06: public-holiday calendar per location."""

    __tablename__ = "holiday"
    __table_args__ = (UniqueConstraint("location_id", "day", name="uq_holiday_day"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("location.id"), nullable=True
    )
    day: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(200))
    is_working_day_override: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# People (Module B)
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "user"
    __table_args__ = (
        UniqueConstraint("org_id", "personnel_number", name="uq_user_personnel"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    personnel_number: Mapped[str] = mapped_column(String(50))
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active/inactive
    role: Mapped[str] = mapped_column(String(20), default="employee")
    team_id: Mapped[str | None] = mapped_column(ForeignKey("team.id"), nullable=True)
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("location.id"), nullable=True
    )
    employment_start: Mapped[date] = mapped_column(Date, default=date.today)
    employment_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    has_login: Mapped[bool] = mapped_column(Boolean, default=True)  # FR-B-02
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(5), default="en")
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # SCIM
    notification_prefs: Mapped[dict] = mapped_column(JSON, default=dict)

    # WT-01 individual opt-out (section 16)
    wt_optout_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    wt_optout_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Credential(Base, TimestampMixin):
    """Passwords, kiosk PINs and QR tokens. Only ever stored as salted hashes
    (NFR-S-02); the plaintext is shown once at generation and never again."""

    __tablename__ = "credential"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    type: Mapped[str] = mapped_column(String(20))  # password | pin | qr
    hash: Mapped[str] = mapped_column(String(255))
    salt: Mapped[str] = mapped_column(String(64))
    lookup: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_rotated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invitation(Base, TimestampMixin):
    """FR-B-03: e-mail invitation with a configurable expiry."""

    __tablename__ = "invitation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkingPattern(Base, TimestampMixin):
    """FR-B-04/05: temporal working pattern. Validity ranges for one user must
    not overlap (section 12.3) and past periods are recalculated using the
    pattern effective on that date."""

    __tablename__ = "working_pattern"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    contracted_hours_per_week: Mapped[float] = mapped_column(Float, default=40.0)
    # Expected minutes per weekday, index 0 = Monday
    expected_minutes: Mapped[list] = mapped_column(
        JSON, default=lambda: [480, 480, 480, 480, 480, 0, 0]
    )
    shift_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "06:00"
    shift_end: Mapped[str | None] = mapped_column(String(5), nullable=True)


# ---------------------------------------------------------------------------
# Attendance capture (Modules C, D, E)
# ---------------------------------------------------------------------------


class BreakType(Base, TimestampMixin):
    """FR-E-01."""

    __tablename__ = "break_type"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(100))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    max_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AttendanceSession(Base, TimestampMixin):
    """A continuous period of recorded presence (glossary, section 23)."""

    __tablename__ = "attendance_session"
    __table_args__ = (Index("ix_session_user_start", "user_id", "start_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)  # UTC
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="timer")  # FR-C-12
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("location.id"), nullable=True
    )
    # DP-13: only the boolean geofence result is retained, never a track.
    within_geofence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    note: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(String(500), default="")
    cost_centre_id: Mapped[str | None] = mapped_column(
        ForeignKey("cost_centre.id"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(32))
    recorded_by_other: Mapped[bool] = mapped_column(Boolean, default=False)  # FR-C-13
    system_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    breaks: Mapped[list["BreakRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class BreakRecord(Base, TimestampMixin):
    """FR-E-02. A break lies entirely within its parent session (section 12.3)."""

    __tablename__ = "break_record"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("attendance_session.id"), index=True
    )
    break_type_id: Mapped[str | None] = mapped_column(
        ForeignKey("break_type.id"), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)

    session: Mapped[AttendanceSession] = relationship(back_populates="breaks")


class TimeEntry(Base, TimestampMixin):
    """Attribution of worked time to a cost centre (FR-C-10 weekly grid).

    A session may be split across cost centres (section 12.2).
    """

    __tablename__ = "time_entry"
    __table_args__ = (Index("ix_entry_user_day", "user_id", "day"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    day: Mapped[date] = mapped_column(Date)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("attendance_session.id"), nullable=True
    )
    cost_centre_id: Mapped[str | None] = mapped_column(
        ForeignKey("cost_centre.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(500), default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="grid")
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(String(32))


class DayAggregate(Base, TimestampMixin):
    """Materialised per-user per-day figures (section 12.2)."""

    __tablename__ = "day_aggregate"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_day_aggregate"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    expected_minutes: Mapped[int] = mapped_column(Integer, default=0)
    present_minutes: Mapped[int] = mapped_column(Integer, default=0)
    break_paid_minutes: Mapped[int] = mapped_column(Integer, default=0)
    break_unpaid_minutes: Mapped[int] = mapped_column(Integer, default=0)
    net_worked_minutes: Mapped[int] = mapped_column(Integer, default=0)
    night_minutes: Mapped[int] = mapped_column(Integer, default=0)
    absence_minutes: Mapped[int] = mapped_column(Integer, default=0)
    absence_paid_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_standard: Mapped[int] = mapped_column(Integer, default=0)
    overtime_night: Mapped[int] = mapped_column(Integer, default=0)
    overtime_weekend: Mapped[int] = mapped_column(Integer, default=0)
    overtime_holiday: Mapped[int] = mapped_column(Integer, default=0)
    overtime_approved_minutes: Mapped[int] = mapped_column(Integer, default=0)
    balance_minutes: Mapped[int] = mapped_column(Integer, default=0)
    first_in: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Absence (Module F)
# ---------------------------------------------------------------------------


class AbsencePolicy(Base, TimestampMixin):
    """FR-F-01."""

    __tablename__ = "absence_policy"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(30), default="")
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True)
    accrual_method: Mapped[str] = mapped_column(String(20), default="annual")
    accrual_rate_days: Mapped[float] = mapped_column(Float, default=25.0)
    carry_over_limit_days: Mapped[float] = mapped_column(Float, default=0.0)
    carry_over_expiry_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    notice_days: Mapped[int] = mapped_column(Integer, default=0)
    requires_document: Mapped[bool] = mapped_column(Boolean, default=False)
    approver_chain: Mapped[list] = mapped_column(
        JSON, default=lambda: ["manager"]
    )  # e.g. ["manager", "hr"]
    min_team_coverage: Mapped[int] = mapped_column(Integer, default=0)  # FR-F-04
    funded_from_time_bank: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class AbsenceRequest(Base, TimestampMixin):
    __tablename__ = "absence_request"
    __table_args__ = (Index("ix_absence_user_dates", "user_id", "start_date"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("absence_policy.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    part_day_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    stage: Mapped[int] = mapped_column(Integer, default=0)  # approver-chain position
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    decision_note: Mapped[str] = mapped_column(Text, default="")
    document_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    deducted_minutes: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    retrospective: Mapped[bool] = mapped_column(Boolean, default=False)


class AbsenceBalance(Base, TimestampMixin):
    __tablename__ = "absence_balance"
    __table_args__ = (
        UniqueConstraint("user_id", "policy_id", "year", name="uq_absence_balance"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("absence_policy.id"))
    year: Mapped[int] = mapped_column(Integer)
    entitlement_minutes: Mapped[int] = mapped_column(Integer, default=0)
    accrued_minutes: Mapped[int] = mapped_column(Integer, default=0)
    carried_over_minutes: Mapped[int] = mapped_column(Integer, default=0)
    adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0)
    adjustment_reason: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# Overtime and time bank (Module G)
# ---------------------------------------------------------------------------


class OvertimeRule(Base, TimestampMixin):
    """FR-G-02."""

    __tablename__ = "overtime_rule"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(100), default="Default")
    daily_threshold_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_threshold_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requires_prior_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    night_start: Mapped[str] = mapped_column(String(5), default="22:00")  # WT-06
    night_end: Mapped[str] = mapped_column(String(5), default="06:00")
    weekend_days: Mapped[list] = mapped_column(JSON, default=lambda: [5, 6])
    time_bank_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    time_bank_cap_minutes: Mapped[int] = mapped_column(Integer, default=6000)
    time_bank_carry_over: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)


class OvertimeApproval(Base, TimestampMixin):
    """FR-G-04: prior approval of overtime hours."""

    __tablename__ = "overtime_approval"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    day: Mapped[date] = mapped_column(Date)
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")


class TimeBankMovement(Base, TimestampMixin):
    """FR-G-05: cumulative hours account."""

    __tablename__ = "time_bank_movement"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    occurred_on: Mapped[date] = mapped_column(Date)
    minutes: Mapped[int] = mapped_column(Integer)  # signed
    kind: Mapped[str] = mapped_column(String(30), default="accrual")
    note: Mapped[str] = mapped_column(Text, default="")
    ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------------------
# Approval and locking (Module H)
# ---------------------------------------------------------------------------


class Period(Base, TimestampMixin):
    __tablename__ = "period"
    __table_args__ = (
        UniqueConstraint("org_id", "start_date", "end_date", name="uq_period"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cutoff_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class Approval(Base, TimestampMixin):
    __tablename__ = "approval"
    __table_args__ = (UniqueConstraint("period_id", "user_id", name="uq_approval"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    period_id: Mapped[str] = mapped_column(ForeignKey("period.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)  # BR-09
    exclusion_reason: Mapped[str] = mapped_column(Text, default="")


class CorrectionRequest(Base, TimestampMixin):
    """FR-H-06 / section 8.4: never overwrite, always supersede."""

    __tablename__ = "correction_request"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    raised_by: Mapped[str] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    proposed_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")


class AttendanceException(Base, TimestampMixin):
    """A detected condition requiring human attention (glossary)."""

    __tablename__ = "attendance_exception"
    __table_args__ = (
        Index("ix_exception_user_day", "user_id", "day"),
        UniqueConstraint("user_id", "day", "type", name="uq_exception_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    day: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    rule_params: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Kiosk (Module D)
# ---------------------------------------------------------------------------


class Kiosk(Base, TimestampMixin):
    __tablename__ = "kiosk"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(200))
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("location.id"), nullable=True
    )
    launch_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_hours: Mapped[int] = mapped_column(Integer, default=24)  # FR-D-08
    assignee_ids: Mapped[list] = mapped_column(JSON, default=list)
    auth_method: Mapped[str] = mapped_column(String(10), default="pin4")
    breaks_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    require_photo: Mapped[bool] = mapped_column(Boolean, default=False)  # FR-D-11
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Audit, exports, notifications, integrations (Modules J, K, L)
# ---------------------------------------------------------------------------


class AuditRecord(Base):
    """FR-L-01/02: append-only. No application role may update or delete."""

    __tablename__ = "audit_record"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class PayrollLayout(Base, TimestampMixin):
    """FR-J-01: configurable delimited layout."""

    __tablename__ = "payroll_layout"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(100))
    columns: Mapped[list] = mapped_column(JSON, default=list)
    delimiter: Mapped[str] = mapped_column(String(3), default=";")
    encoding: Mapped[str] = mapped_column(String(20), default="utf-8")
    date_format: Mapped[str] = mapped_column(String(20), default="%Y-%m-%d")
    duration_format: Mapped[str] = mapped_column(String(10), default="decimal")
    include_header: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)


class PayrollExport(Base, TimestampMixin):
    __tablename__ = "payroll_export"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    period_id: Mapped[str] = mapped_column(ForeignKey("period.id"))
    layout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    generated_by: Mapped[str] = mapped_column(String(32))
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    rows_json: Mapped[list] = mapped_column(JSON, default=list)
    period_locked: Mapped[bool] = mapped_column(Boolean, default=False)


class SavedReport(Base, TimestampMixin):
    """FR-I-09 saved filter sets, FR-I-11 scheduling, FR-I-12 share links."""

    __tablename__ = "saved_report"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    owner_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    report_type: Mapped[str] = mapped_column(String(40))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule_cron: Mapped[str | None] = mapped_column(String(60), nullable=True)
    schedule_recipients: Mapped[list] = mapped_column(JSON, default=list)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Notification(Base):
    """Module K. Content carries no more personal data than necessary (FR-K-04):
    the payload is a message and a deep link, not the data itself."""

    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(20), default="inapp")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(500), default="")
    link: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiKey(Base, TimestampMixin):
    """FR-J-05: scoped API credentials."""

    __tablename__ = "api_key"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    hash: Mapped[str] = mapped_column(String(255))
    salt: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Webhook(Base, TimestampMixin):
    """FR-J-06."""

    __tablename__ = "webhook"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list] = mapped_column(JSON, default=list)
    secret: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_delivery"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhook.id"))
    event: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    signature: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RuleParamVersion(Base):
    """Section 16: rule parameters are versioned; historic evaluations reflect
    the parameters in force at the time."""

    __tablename__ = "rule_param_version"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    effective_from: Mapped[date] = mapped_column(Date, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
