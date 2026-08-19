"""Request and response contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Authentication ---------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str
    mfa_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class PasswordChange(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=10)


# --- Organisation -----------------------------------------------------------


class OrganisationUpdate(BaseModel):
    name: str | None = None
    country: str | None = None
    timezone: str | None = None
    week_start: int | None = Field(default=None, ge=0, le=6)
    date_format: str | None = None
    time_format: Literal["12h", "24h"] | None = None
    duration_format: Literal["hm", "decimal"] | None = None
    period_type: Literal["weekly", "biweekly", "semimonthly", "monthly"] | None = None
    submission_cutoff_days: int | None = Field(default=None, ge=0, le=31)
    channel_timer: bool | None = None
    channel_manual: bool | None = None
    channel_grid: bool | None = None
    channel_kiosk: bool | None = None
    channel_mobile: bool | None = None
    require_cost_centre: bool | None = None
    require_project: bool | None = None
    require_note: bool | None = None
    rounding_minutes: Literal[0, 1, 5, 10, 15] | None = None
    rounding_direction: Literal["nearest", "up", "down"] | None = None
    max_session_hours: int | None = Field(default=None, ge=1, le=24)
    auto_stop_runaway: bool | None = None
    auto_break_after_minutes: int | None = Field(default=None, ge=0)
    auto_break_minutes: int | None = Field(default=None, ge=0)
    managers_may_launch_kiosk: bool | None = None
    auto_approve_after_days: int | None = Field(default=None, ge=0)
    retention_years: int | None = Field(default=None, ge=1, le=50)


class LocationIn(BaseModel):
    name: str
    address: str = ""
    timezone: str = "Europe/Bratislava"
    geo_lat: float | None = None
    geo_lng: float | None = None
    geo_radius_m: int | None = None


class TeamIn(BaseModel):
    name: str
    parent_team_id: str | None = None
    manager_user_id: str | None = None


class CostCentreIn(BaseModel):
    code: str
    name: str


class HolidayIn(BaseModel):
    day: date
    name: str
    location_id: str | None = None
    is_working_day_override: bool = False


class HolidayImport(BaseModel):
    year: int
    country: str = "SK"
    location_id: str | None = None


# --- People -----------------------------------------------------------------


class UserIn(BaseModel):
    personnel_number: str
    first_name: str
    last_name: str
    email: EmailStr | None = None
    role: Literal["owner", "admin", "hr", "manager", "employee", "limited"] = "employee"
    team_id: str | None = None
    location_id: str | None = None
    employment_start: date
    employment_end: date | None = None
    has_login: bool = True
    language: str = "en"

    @field_validator("email")
    @classmethod
    def limited_members_need_no_email(cls, value, info):
        return value


class UserUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr | None = None
    role: Literal["owner", "admin", "hr", "manager", "employee", "limited"] | None = None
    team_id: str | None = None
    location_id: str | None = None
    employment_end: date | None = None
    status: Literal["active", "inactive"] | None = None
    language: str | None = None
    wt_optout_from: date | None = None
    wt_optout_ref: str | None = None


class WorkingPatternIn(BaseModel):
    valid_from: date
    contracted_hours_per_week: float = 40.0
    expected_minutes: list[int] = Field(min_length=7, max_length=7)
    shift_start: str | None = None
    shift_end: str | None = None


class GroupIn(BaseModel):
    name: str
    member_ids: list[str] = []


# --- Attendance -------------------------------------------------------------


class TimerStart(BaseModel):
    description: str = ""
    cost_centre_id: str | None = None
    note: str = ""
    source: Literal["timer", "mobile", "api"] = "timer"
    device_id: str | None = None
    start_at: datetime | None = None
    geo_lat: float | None = None
    geo_lng: float | None = None


class TimerStop(BaseModel):
    end_at: datetime | None = None
    note: str = ""


class SessionIn(BaseModel):
    user_id: str | None = None
    start_at: datetime
    end_at: datetime | None = None
    description: str = ""
    cost_centre_id: str | None = None
    note: str = ""
    source: Literal["manual", "api", "mobile"] = "manual"
    reason: str = ""


class SessionUpdate(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    description: str | None = None
    cost_centre_id: str | None = None
    note: str | None = None
    reason: str = ""
    confirm: bool | None = None


class BreakStart(BaseModel):
    break_type_id: str | None = None
    start_at: datetime | None = None


class BreakStop(BaseModel):
    end_at: datetime | None = None


class BreakTypeIn(BaseModel):
    name: str
    is_paid: bool = False
    max_minutes: int | None = None


class GridCell(BaseModel):
    day: date
    cost_centre_id: str | None = None
    minutes: int = Field(ge=0, le=24 * 60)
    description: str = ""


class GridSave(BaseModel):
    user_id: str | None = None
    cells: list[GridCell]


# --- Kiosk ------------------------------------------------------------------


class KioskIn(BaseModel):
    name: str
    location_id: str | None = None
    assignee_ids: list[str] = []
    auth_method: Literal["pin4", "pin6", "qr"] = "pin4"
    breaks_enabled: bool = True
    session_hours: int = 24
    require_photo: bool = False


class KioskAuth(BaseModel):
    user_id: str
    pin: str | None = None
    qr_token: str | None = None


class KioskEvent(BaseModel):
    user_id: str
    pin: str | None = None
    qr_token: str | None = None
    action: Literal["clock_in", "clock_out", "break_start", "break_end"]
    break_type_id: str | None = None
    occurred_at: datetime | None = None
    idempotency_key: str
    device_id: str | None = None
    geo_lat: float | None = None
    geo_lng: float | None = None


class KioskBatch(BaseModel):
    events: list[KioskEvent]


# --- Absence ----------------------------------------------------------------


class AbsencePolicyIn(BaseModel):
    name: str
    code: str = ""
    is_paid: bool = True
    accrual_method: Literal["annual", "monthly", "unlimited"] = "annual"
    accrual_rate_days: float = 25.0
    carry_over_limit_days: float = 0.0
    carry_over_expiry_month: int | None = None
    allow_negative: bool = False
    notice_days: int = 0
    requires_document: bool = False
    approver_chain: list[str] = ["manager"]
    min_team_coverage: int = 0
    funded_from_time_bank: bool = False


class AbsenceRequestIn(BaseModel):
    user_id: str | None = None
    policy_id: str
    start_date: date
    end_date: date
    part_day_hours: float | None = None
    reason: str = ""
    document_ref: str | None = None


class AbsenceDecision(BaseModel):
    note: str = ""


class BalanceAdjustment(BaseModel):
    user_id: str
    policy_id: str
    year: int
    minutes: int
    reason: str = Field(min_length=3)


# --- Overtime ---------------------------------------------------------------


class OvertimeRuleIn(BaseModel):
    daily_threshold_minutes: int | None = None
    weekly_threshold_minutes: int | None = None
    requires_prior_approval: bool = False
    night_start: str = "22:00"
    night_end: str = "06:00"
    weekend_days: list[int] = [5, 6]
    time_bank_enabled: bool = False
    time_bank_cap_minutes: int = 6000
    time_bank_carry_over: bool = True


class OvertimeRequest(BaseModel):
    user_id: str | None = None
    day: date
    minutes: int
    reason: str = ""


# --- Approval ---------------------------------------------------------------


class SubmitRequest(BaseModel):
    period_id: str | None = None
    day: date | None = None


class DecisionRequest(BaseModel):
    period_id: str
    user_ids: list[str]
    reason: str = ""


class LockRequest(BaseModel):
    period_id: str
    reason: str = ""


class ExclusionRequest(BaseModel):
    period_id: str
    user_id: str
    reason: str = Field(min_length=3)


class CorrectionIn(BaseModel):
    entity_type: Literal["attendance_session", "time_entry"]
    entity_id: str
    proposed: dict[str, Any]
    reason: str = Field(min_length=3)


class CorrectionDecision(BaseModel):
    note: str = ""


class ExceptionResolution(BaseModel):
    note: str = Field(min_length=3)


# --- Reporting --------------------------------------------------------------


class ReportFilters(BaseModel):
    start: date
    end: date
    user_ids: list[str] | None = None
    team_ids: list[str] | None = None
    location_ids: list[str] | None = None
    cost_centre_ids: list[str] | None = None
    only_exceptions: bool = False
    group_by: str | None = None
    status: str | None = None


class SavedReportIn(BaseModel):
    name: str
    report_type: str
    filters: dict[str, Any]
    schedule_cron: str | None = None
    schedule_recipients: list[str] = []


class ShareRequest(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=90)


# --- Payroll ----------------------------------------------------------------


class PayrollLayoutIn(BaseModel):
    name: str
    columns: list[str]
    delimiter: str = ";"
    encoding: str = "utf-8"
    date_format: str = "%Y-%m-%d"
    duration_format: Literal["decimal", "hm", "minutes"] = "decimal"
    include_header: bool = True


class PayrollExportRequest(BaseModel):
    period_id: str
    layout_id: str | None = None
    confirm_unlocked: bool = False


# --- Integrations -----------------------------------------------------------


class ApiKeyIn(BaseModel):
    name: str
    scopes: list[str] = ["*"]


class WebhookIn(BaseModel):
    url: str
    events: list[str] = []


class RuleParamsIn(BaseModel):
    effective_from: date
    params: dict[str, int]


class NotificationPrefs(BaseModel):
    prefs: dict[str, Any]
