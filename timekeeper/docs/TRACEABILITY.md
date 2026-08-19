# Traceability matrix

Every requirement ID in the specification, mapped to where it is implemented
and where it is proved. `—` in the test column means the requirement is
structural (a data-model or configuration property) and is exercised
indirectly by the tests of the requirements that depend on it.

Status: **Done** unless stated otherwise. MoSCoW priority is shown as in the
specification.

---

## Module A — Organisation and configuration

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-A-01 | M | `models.Organisation`; every table carries `org_id` and every query is scoped by it | `test_permissions.py` (cross-org isolation via `org_id` filters) |
| FR-A-02 | M | `models.Team.parent_team_id`; `security.descendant_team_ids` walks the hierarchy | `test_workflows.py::test_a_team_cannot_be_its_own_ancestor`, `test_permissions.py::test_manager_sees_only_their_own_team` |
| FR-A-03 | M | `models.Location`, `routers/org.py` locations CRUD | `test_workflows.py::test_dp13_geofence_stores_only_the_boolean_result` |
| FR-A-04 | M | `Organisation.week_start/date_format/time_format/duration_format`; `timeutil.format_duration` | `test_calc.py::test_format_duration`, `test_reports.py::test_csv_totals_use_the_configured_duration_format` |
| FR-A-05 | M | `Organisation.period_type`, `timeutil.period_bounds`, `services/periods.py` | `test_calc.py::test_period_bounds` |
| FR-A-06 | M | `models.Holiday`, `services/holidays.py` (computed, incl. Easter), `POST /api/org/holidays/import` | `test_workflows.py::test_fr_a06_holiday_import` |
| FR-A-07 | S | `Organisation.channel_*`, `routers/attendance.channel_enabled` | `test_workflows.py::test_fr_a07_a_disabled_channel_is_refused` |
| FR-A-08 | S | `Organisation.require_*`, `routers/attendance.validate_mandatory` | `test_workflows.py::test_fr_a08_mandatory_fields_are_enforced` |
| FR-A-09 | S | `timeutil.round_timestamp`, applied in `routers/attendance.apply_rounding` | `test_calc.py::test_rounding` (9 cases), `test_workflows.py::test_fr_a09_rounding_is_applied_at_clock_in` |

## Module B — People management

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-B-01 | M | `models.User`, `POST /api/users` | `test_workflows.py::test_fr_b03_invitation_round_trip` |
| FR-B-02 | M | `User.has_login=False` + kiosk PIN credential; creation guard in `routers/users.py` | `test_workflows.py::test_fr_b02_a_limited_member_cannot_have_a_login`, all US-01 tests |
| FR-B-03 | M | `models.Invitation`, `POST /api/users/{id}/invite`, `/api/auth/invitation/{token}` | `test_workflows.py::test_fr_b03_invitation_round_trip` |
| FR-B-04 | M | `models.WorkingPattern` with per-weekday expected minutes | `test_calc.py::test_part_time_pattern_changes_expected_hours` |
| FR-B-05 | M | Temporal validity ranges; `calc.pattern_for` selects by date; overlap guard in `POST /api/users/{id}/patterns` | `test_calc.py::test_fr_b05_recalculation_uses_the_pattern_in_force`, `test_workflows.py::test_overlapping_patterns_are_refused` |
| FR-B-06 | M | `POST /api/users/{id}/deactivate`; history preserved | `test_workflows.py::test_fr_b06_deactivation_preserves_history` |
| FR-B-07 | S | `models.UserGroup`, `/api/org/groups` | — |
| FR-B-08 | S | `POST /api/users/import` with `dry_run` preview and per-line validation | `test_workflows.py::test_fr_b08_bulk_import_dry_run_then_commit` |
| FR-B-09 | C | `/api/integrations/sso/metadata`, `/api/integrations/scim/v2/Users`. Assertion consumption is deployment-specific and documented rather than hard-coded | `test_workflows.py::test_scim_and_sso_descriptors` |

## Module C — Attendance capture

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-C-01 | M | `POST /api/attendance/start` | `test_user_stories.py::test_us02_ac1_...` |
| FR-C-02 | M | Server-side session state; the client renders elapsed time from `start_at` | `test_user_stories.py::test_us02_ac2_the_timer_is_server_side_...` |
| FR-C-03 | M | Timer/manual toggle in the client; `POST /api/attendance/sessions` | `test_user_stories.py::test_manual_entry_cannot_overlap_an_existing_one` |
| FR-C-04 | S | `PUT /api/attendance/sessions/{id}` accepts `start_at` on a running session | `test_user_stories.py::test_us02_ac3_editing_the_start_time_...` |
| FR-C-05 | S | `POST /api/attendance/sessions/{id}/continue` | — (client flow; endpoint covered by the overlap guard tests) |
| FR-C-06 | M | `.../duplicate` and `DELETE /api/attendance/sessions/{id}` | `test_workflows.py::test_fr_l01_every_mutation_is_audited` |
| FR-C-07 | M | `GET /api/attendance/tracker` groups by day with per-day and period totals | `test_user_stories.py::test_us02_ac1_...` |
| FR-C-08 | M | `routers/attendance.assert_no_overlap` returns the conflict and the resolutions | `test_user_stories.py::test_manual_entry_cannot_overlap_an_existing_one`, `test_us02_ac4_...` |
| FR-C-09 | M | `services/batch.handle_runaway_sessions`: notify, flag, optional auto-stop at the shift end, marked system-generated and unconfirmed | `test_batch.py::test_auto_stop_marks_the_entry_system_generated_and_unconfirmed`, `::test_auto_stop_uses_the_configured_shift_end`, `test_user_stories.py::test_us02_ac5_...` |
| FR-C-10 | M | `GET/POST /api/attendance/grid` with row, column and grand totals | `test_workflows.py::test_fr_c10_weekly_grid_round_trip` |
| FR-C-11 | C | `static/js/views/tracker.js` — `s` start/stop, `n` new entry, `m` switch mode | — (client-side) |
| FR-C-12 | M | `AttendanceSession.source/device_id/ip` | `test_reports.py::test_detailed_report_lists_sessions_and_grid_entries` |
| FR-C-13 | S | `SessionIn.user_id` + `recorded_by_other` flag + notification to the employee | `test_user_stories.py::test_us06_ac3_...` (HR records on behalf), `test_workflows.py::test_fr_k01_...` |

## Module D — Kiosk

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-D-01 | M | `POST /api/kiosks` | `test_permissions.py::test_managers_may_not_launch_a_kiosk_by_default` |
| FR-D-02 | M | `launch_token`, `/kiosk.html?token=…`, full-screen client with no navigation | `test_user_stories.py::test_kiosk_token_is_revocable` |
| FR-D-03 | M | `auth_method` = `pin4` / `pin6` / `qr`; `routers/kiosk.authenticate` | `test_user_stories.py::test_us01_ac2_...` |
| FR-D-04 | M | `POST /api/users/{id}/pin` generates, enforces uniqueness in the workspace, stores a PBKDF2 salted hash and returns the value exactly once | `test_workflows.py::test_fr_l02_credentials_are_never_written_to_the_audit_log` |
| FR-D-05 | M | `GET /api/kiosk/session` roster + single-step confirm | `test_user_stories.py::test_us01_ac1_...` |
| FR-D-06 | S | Roster `status` field, colour-coded tiles | `test_user_stories.py::test_us01_ac4_...` |
| FR-D-07 | M | `break_start` / `break_end` kiosk actions with break-type selection | `test_workflows.py::test_break_lifecycle_affects_net_time` |
| FR-D-08 | S | `token_expires_at`, `session_hours`, `POST /api/kiosks/{id}/relaunch` | `test_user_stories.py::test_kiosk_token_is_revocable` |
| FR-D-09 | S | Client queue in `localStorage`; `POST /api/kiosk/sync` replays with original timestamps; idempotency keys | `test_user_stories.py::test_us01_ac5_offline_events_sync_...`, `::test_us01_ac5_replayed_events_are_not_double_booked` |
| FR-D-10 | M | The roster payload is exactly `{id, name, status, since}` | `test_user_stories.py::test_us01_ac1_roster_exposes_nothing_beyond_name_and_status` |
| FR-D-11 | C | `Kiosk.require_photo`, off by default, with the legal-basis warning in the admin UI. Image capture and storage is deliberately **not** implemented pending the DPIA | — |

## Module E — Breaks

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-E-01 | M | `models.BreakType` (name, paid flag, maximum) | `test_workflows.py::test_break_lifecycle_affects_net_time` |
| FR-E-02 | M | Break start/stop from web and kiosk; unpaid time excluded from net | `test_calc.py::test_br01_unpaid_break_is_deducted`, `::test_br01_paid_break_counts_as_worked` |
| FR-E-03 | S | `Organisation.auto_break_after_minutes/auto_break_minutes` in `calc.compute_day` | `test_calc.py::test_automatic_break_deduction` (3 cases) |
| FR-E-04 | M | WT-04 evaluation in `rules.evaluate_day` | `test_rules.py::test_wt04_break_shortfall_is_raised` (3 cases) |
| FR-E-05 | M | Guard in `POST /api/attendance/breaks/start` | `test_workflows.py::test_fr_e05_a_break_cannot_start_without_a_session` |

## Module F — Absence and time off

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-F-01 | M | `models.AbsencePolicy` — all thirteen attributes of the requirement | `test_workflows.py::test_fr_f01_two_stage_approver_chain`, `test_absence_and_payroll.py` (accrual, carry-over, notice, negative) |
| FR-F-02 | M | `POST /api/absence/requests`, incl. `part_day_hours` | `test_absence_and_payroll.py::test_requested_minutes_for_a_part_day` |
| FR-F-03 | M | `absence.validate` — balance, notice, holidays, existing absences | `test_absence_and_payroll.py` (5 validation tests), `test_user_stories.py::test_us05_ac2/ac3` |
| FR-F-04 | S | `absence.team_coverage_warning` | `test_absence_and_payroll.py::test_fr_f04_minimum_team_coverage_warning` |
| FR-F-05 | M | `GET /api/absence/calendar`; approved absence suppresses BR-06 | `test_rules.py::test_fr_f05_approved_absence_suppresses_unexplained_absence`, `test_user_stories.py::test_us05_ac4_...` |
| FR-F-06 | M | `absence.balance_for` — entitlement, accrued, taken, planned, remaining | `test_absence_and_payroll.py::test_taken_planned_and_pending_are_reported_separately` |
| FR-F-07 | M | `POST /api/absence/balance-adjustment`, reason mandatory, audited | `test_workflows.py::test_fr_f07_manual_balance_adjustment_requires_a_reason` |
| FR-F-08 | M | HR/manager may raise a request for another employee; approved on entry | `test_workflows.py::test_fr_f08_hr_records_absence_retrospectively` |
| FR-F-09 | C | `AbsencePolicy.funded_from_time_bank` writes a negative `TimeBankMovement` | `test_absence_and_payroll.py::test_time_bank_balance_is_the_sum_of_movements` |

## Module G — Overtime and capacity

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-G-01 | M | `calc.compute_day` → `DayAggregate`; `calc.period_totals` | `test_calc.py::test_period_totals_sum_the_days` |
| FR-G-02 | M | `models.OvertimeRule` — daily and weekly thresholds, prior approval, night window, weekend days | `test_calc.py::test_daily_threshold_overrides_expected`, `::test_weekly_threshold_adds_to_the_last_worked_day` |
| FR-G-03 | M | Four mutually exclusive categories with a documented priority | `test_calc.py::test_overtime_standard`, `::test_br04_night_hours_and_night_overtime`, `::test_weekend_work_is_weekend_overtime`, `::test_br05_public_holiday_work_is_holiday_overtime`, `::test_categories_are_mutually_exclusive` |
| FR-G-04 | S | `models.OvertimeApproval`; unapproved overtime recorded, reported, excluded from the export | `test_calc.py::test_unapproved_overtime_is_excluded_until_approved`, `test_workflows.py::test_fr_g04_overtime_approval_flow` |
| FR-G-05 | S | `models.TimeBankMovement`, capped, credited on period approval | `test_workflows.py::test_fr_g05_approved_overtime_feeds_the_time_bank` |
| FR-G-06 | S | `GET /api/dashboard/me` returns period balance and time-bank balance | `test_user_stories.py` (dashboard used throughout) |

## Module H — Approval and locking

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-H-01 | M | `POST /api/approvals/submit`; period becomes read-only to the employee | `test_user_stories.py::test_us03_ac3_...` |
| FR-H-02 | M | Approve, or reject with a mandatory reason | `test_user_stories.py::test_us04_ac4_...` |
| FR-H-03 | S | Bulk approval from the queue in one action | `test_user_stories.py::test_us04_ac3_...` |
| FR-H-04 | M | `rules.blocking_exceptions` blocks submission; the evaluation is committed so the employee can act on it | `test_user_stories.py::test_us04_ac2_...` |
| FR-H-05 | M | `POST /api/approvals/lock` / `unlock`, both audited, unlock requires a reason | `test_user_stories.py::test_us03_ac3_...`, `test_us06_ac3_...` |
| FR-H-06 | M | `models.CorrectionRequest`; approval writes a new version and sets `superseded_by` | `test_user_stories.py::test_us03_ac3_a_locked_period_offers_a_correction_request` |
| FR-H-07 | S | `batch.submission_reminders` — escalating at 3 days, 1 day, cut-off, overdue | `test_batch.py::test_submission_reminder_is_sent_near_the_cut_off` (3 cases) |
| FR-H-08 | C | `batch.auto_approve`, off unless `auto_approve_after_days` is set, never over a blocking exception | `test_batch.py::test_auto_approval_only_when_enabled`, `::test_auto_approval_skips_blocking_exceptions` |

## Module I — Reporting and analytics

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-I-01 | M | `reports.attendance_report` — every column of the requirement | `test_reports.py::test_attendance_report_has_one_row_per_employee_per_day`, `::test_attendance_report_flags_days_with_no_record` |
| FR-I-02 | M | `reports.summary_report` with five groupings | `test_reports.py::test_summary_report_groupings` (5 cases) |
| FR-I-03 | M | `reports.weekly_report` | `test_reports.py::test_weekly_report_is_a_matrix_of_employees_by_day` |
| FR-I-04 | M | `reports.detailed_report` — sessions and grid entries, with source, device, version | `test_reports.py::test_detailed_report_lists_sessions_and_grid_entries` |
| FR-I-05 | M | `reports.absence_report` | `test_reports.py::test_absence_report_shows_balances` |
| FR-I-06 | M | `reports.overtime_report` — by category, approved and unapproved | `test_reports.py::test_overtime_report_splits_the_categories` |
| FR-I-07 | M | `reports.compliance_report` with the parameters in force recorded per row | `test_reports.py::test_compliance_report_is_the_evidence_artefact`, `test_user_stories.py::test_us07_...` |
| FR-I-08 | M | `reports.live_board` | `test_reports.py::test_live_board_counts_states`, `test_workflows.py::test_live_board_states` |
| FR-I-09 | M | `ReportFilters` + `models.SavedReport` | `test_reports.py::test_population_is_narrowed_by_team_and_employee`, `test_workflows.py::test_fr_i09_and_i12_...` |
| FR-I-10 | M | `services/exports.py` — CSV, XLSX, PDF through one formatter | `test_reports.py::test_csv_export_matches_the_report`, `::test_xlsx_and_pdf_exports_are_produced`, `test_user_stories.py::test_us06_ac2_...` |
| FR-I-11 | S | `SavedReport.schedule_cron` + `batch.deliver_scheduled_reports` (delivery adapter logs; SMTP is a deployment concern) | `test_batch.py::test_cron_matcher` (9 cases), `::test_a_due_scheduled_report_is_delivered_once` |
| FR-I-12 | C | `share_token` + `share_expires_at`; the link runs with the sharer's visibility | `test_workflows.py::test_fr_i09_and_i12_...`, `::test_expired_share_link_is_refused` |

## Module J — Payroll export and integration

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-J-01 | M | `models.PayrollLayout` — columns, order, delimiter, encoding, date and duration format | `test_absence_and_payroll.py::test_layout_controls_columns_delimiter_and_duration_format` |
| FR-J-02 | M | `payroll.build_rows` — normal hours, each overtime category, paid absence by policy, unpaid absence, unpaid break deduction | `test_absence_and_payroll.py::test_payroll_rows_separate_normal_hours_from_overtime`, `::test_paid_absence_is_broken_down_by_policy` |
| FR-J-03 | S | `payroll.reconcile` — per-employee, per-field previous/current/delta, plus added and removed | `test_absence_and_payroll.py::test_export_checksum_changes_only_when_the_content_does`, `test_user_stories.py::test_us06_ac3_...` |
| FR-J-04 | M | Every export stored with actor, time, scope and SHA-256; audited; re-downloadable byte for byte | `test_user_stories.py::test_fr_j04_exports_are_logged_and_re_downloadable` |
| FR-J-05 | S | `/api/v1/*` plus API keys with scopes that narrow, never widen | `test_permissions.py::test_api_key_scopes_narrow_but_never_widen`, `::test_revoked_api_key_stops_working` |
| FR-J-06 | C | `services/webhooks.py` — persisted, HMAC-SHA256 signed, dispatched by the batch | `test_workflows.py::test_fr_j06_webhooks_are_queued_with_a_signature` |
| FR-J-07 | C | `/api/integrations/calendar.ics` | `test_workflows.py::test_fr_j07_calendar_feed` |

## Module K — Notifications

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-K-01 | M | Employee notices: runaway timer, period due, entry amended, absence decided | `test_workflows.py::test_fr_k01_...`, `test_batch.py::test_runaway_session_notifies_the_employee` |
| FR-K-02 | M | Manager notices: timesheet awaiting, absence awaiting, exception raised, absent without notice | `test_batch.py::test_manager_is_told_when_an_exception_is_raised_in_the_team`, `::test_absent_without_notice_alerts_the_manager` |
| FR-K-03 | S | `notifications.CATALOGUE` with a mandatory subset the user cannot switch off | `test_workflows.py::test_fr_k03_a_mandatory_notification_cannot_be_switched_off` |
| FR-K-04 | M | Payload is a short title, a short body and a deep link — never the data itself | `test_workflows.py::test_fr_k04_notifications_link_rather_than_embed_data` |

## Module L — Audit and data governance

| ID | Pri | Implementation | Test |
|---|:--:|---|---|
| FR-L-01 | M | `audit.record` on every mutation, with actor, UTC time, action, entity, before and after | `test_workflows.py::test_fr_l01_every_mutation_is_audited` |
| FR-L-02 | M | Only an insert path exists; credential material is redacted. Enforce `DENY UPDATE/DELETE` at the database role in production (README) | `test_workflows.py::test_fr_l02_no_endpoint_can_modify_the_audit_log`, `::test_fr_l02_credentials_are_never_written_to_the_audit_log` |
| FR-L-03 | M | `GET /api/audit` with filters, `GET /api/audit/export` | `test_workflows.py::test_fr_l03_audit_log_is_searchable_and_exportable` |
| FR-L-04 | M | `batch.enforce_retention` — configurable years, deletion logged | `test_batch.py::test_retention_keeps_recent_records_and_removes_old_ones`, `test_workflows.py::test_fr_l04_...` |
| FR-L-05 | M | `GET /api/users/{id}/data-export` | `test_workflows.py::test_fr_l05_subject_access_export`, `::test_an_employee_cannot_export_someone_else` |

---

## Business rules (section 13)

| ID | Implementation | Test |
|---|---|---|
| BR-01 | `calc.compute_day`: net = present − unpaid breaks; paid breaks count | `test_calc.py::test_br01_unpaid_break_is_deducted`, `::test_br01_paid_break_counts_as_worked` |
| BR-02 | `figures.balance = net + paid absence − expected` | `test_calc.py::test_br02_balance_surplus_and_deficit`, `::test_br02_paid_absence_fills_the_day` |
| BR-03 | Overtime derives from `net`, never `present` | `test_calc.py::test_br03_overtime_is_on_net_not_gross` |
| BR-04 | `night_minutes` computed from the configurable night window | `test_calc.py::test_br04_night_hours_and_night_overtime` |
| BR-05 | Holiday ⇒ expected 0 and all worked time is holiday overtime | `test_calc.py::test_br05_public_holiday_work_is_holiday_overtime` |
| BR-06 | `UNEXPLAINED_ABSENCE` when expected > 0 with no attendance and no absence | `test_rules.py::test_br06_unexplained_absence` |
| BR-07 | Rounding at clock-in/out only, same direction both ends | `test_calc.py::test_br07_rounding_is_symmetric_by_default` |
| BR-08 | Balance consumed at approval; planned shown apart from taken | `test_user_stories.py::test_br08_balance_is_consumed_at_approval_not_at_request` |
| BR-09 | Lock refused unless everyone is approved or explicitly excluded with a reason | `test_user_stories.py::test_us03_ac3_...` (exclusion path), `test_permissions.py` |
| BR-10 | Only approved overtime reaches the export where prior approval is required | `test_absence_and_payroll.py::test_br10_unapproved_overtime_is_excluded_from_the_export` |
| BR-11 | Retrospective pattern change recalculates and reports affected locked periods | `test_workflows.py::test_fr_b05_retrospective_pattern_change_recalculates` |
| BR-12 | A limited member is a kiosk identity: `has_login=False`, creation guard | `test_workflows.py::test_fr_b02_a_limited_member_cannot_have_a_login` |

## Working-time rules (section 16)

| ID | Default | Configurable | Implementation | Test |
|---|---|:--:|---|---|
| WT-01 | 48 h average / 4 months | yes | `rules.evaluate_rolling`; individual opt-out per employee | `test_rules.py::test_wt01_average_weekly_breach`, `::test_wt01_individual_opt_out_suppresses_the_breach`, `::test_wt01_normal_hours_do_not_breach` |
| WT-02 | 11 h daily rest | yes | `rules.evaluate_day` | `test_rules.py::test_wt02_min_rest_breach`, `::test_wt02_no_breach_with_eleven_hours` |
| WT-03 | 24 h weekly rest (+ daily) | yes | `rules.evaluate_rolling` | `test_rules.py::test_wt03_weekly_rest_breach` |
| WT-04 | break after 6 h, ≥ 30 min | yes | `rules.evaluate_day` | `test_rules.py::test_wt04_break_shortfall_is_raised`, `::test_wt04_threshold_is_configurable` |
| WT-05 | 8 h average night work | yes | `rules.evaluate_rolling` | covered by `evaluate_rolling` tests |
| WT-06 | 22:00–06:00 | yes | `OvertimeRule.night_start/night_end` | `test_calc.py::test_br04_night_hours_and_night_overtime` |
| WT-07 | 4 weeks annual leave | yes | `DEFAULT_PARAMS["wt07_annual_leave_weeks"]`, checked against policy entitlement | `test_workflows.py::test_rule_parameters_are_saved_as_a_version` |

Engine requirements: each rule produces an `AttendanceException` with severity
and a blocking flag; rules run on write and in the nightly batch; parameters
are versioned so a historic evaluation uses the values then in force; opt-outs
are recorded per employee with a date and a reference; the compliance report is
exportable for any historic period.
Tests: `test_rules.py::test_rule_parameters_are_versioned`,
`::test_us07_ac4_resolved_exceptions_are_retained_not_deleted`,
`::test_break_shortfall_is_not_blocking`.

## Roles and permissions (section 9)

The matrix of section 9.1 is transcribed cell by cell into
`tests/test_permissions.py::MATRIX` and asserted against
`security.PERMISSIONS`, then proved over HTTP by nine further tests. The two
footnotes are implemented: a manager's edit always creates an audited revision
and notifies the employee, and whether managers may launch a kiosk is an
organisation setting that defaults to administrators only.

## Privacy and data protection (section 15)

| ID | Implementation | Test |
|---|---|---|
| DP-01 | Lawful bases documented per purpose in `/api/privacy/notice` | `test_workflows.py::test_dp05_privacy_notice_is_available_in_product` |
| DP-02 | DPIA is a launch dependency; photo capture ships off with the warning in the UI | — (process) |
| DP-03 | Screenshots, application monitoring and continuous location are **not implemented** | `test_workflows.py::test_dp05_...` asserts the notice says so |
| DP-04 | No performance-profiling feature exists; the notice states the purpose limit | — |
| DP-05 | `GET /api/privacy/notice`, linked from the employee dashboard | `test_workflows.py::test_dp05_...` |
| DP-06 | `GET /api/users/{id}/data-export` (machine-readable) | `test_workflows.py::test_fr_l05_subject_access_export` |
| DP-07 | Correction requests, with the outcome recorded | `test_user_stories.py::test_us03_ac3_...` |
| DP-08 | `batch.enforce_retention`, period configurable | `test_batch.py::test_retention_keeps_recent_records_and_removes_old_ones` |
| DP-09 | Least privilege in `security.visible_user_ids`; access to another employee's detail is logged | `test_permissions.py::test_manager_sees_only_their_own_team`, `::test_dp09_viewing_another_employee_is_logged` |
| DP-10 | Deployment constraint, documented in the README | — |
| DP-11 | The system flags; a person always decides. No automatic disciplinary action exists | — |
| DP-12 | Monitoring features are per-kiosk switches, off by default | — |
| DP-13 | Geofence stores only the boolean and the site id | `test_workflows.py::test_dp13_geofence_stores_only_the_boolean_result` |

## Non-functional requirements (section 14)

| ID | How it is met | Evidence |
|---|---|---|
| NFR-P-01 | The client is ~40 KB of dependency-free ES modules; the tracker is one request | Manual — measure on the target network |
| NFR-P-02 | Clock-in is a single insert plus a same-day recompute | Manual load test required before launch |
| NFR-P-03 | Reports read materialised `DayAggregate` rows, not raw sessions | `test_reports.py` (correctness); a 500-employee load test is a launch gate |
| NFR-P-04 | Kiosk writes are independent inserts; the reporting path is read-only | Load test required |
| NFR-A-01/02 | Deployment concern — documented in the README | — |
| NFR-A-03 | Kiosk offline queue | `test_user_stories.py::test_us01_ac5_...` |
| NFR-A-04 | Reports never lock the write path; the batch is separable from the web tier | — |
| NFR-S-01 | TLS terminated in front; encryption at rest is a deployment setting | README |
| NFR-S-02 | PBKDF2-HMAC-SHA256, 210 000 iterations, per-credential salt, never logged, shown once | `test_workflows.py::test_fr_l02_credentials_are_never_written_to_the_audit_log` |
| NFR-S-03 | Every endpoint re-derives scope server-side | the whole of `test_permissions.py` |
| NFR-S-04 | TOTP available to all; mandatory for owner, admin and HR | `test_workflows.py::test_mfa_is_mandatory_for_privileged_roles`, `::test_mfa_enrolment_and_login` |
| NFR-S-05 | Lock-out on login and on kiosk PIN entry | `test_workflows.py::test_repeated_failed_logins_lock_the_account`, `test_user_stories.py::test_us01_ac3_...` |
| NFR-S-06 | Kiosk tokens expire and are revocable; a leaked token reaches nothing but the roster | `test_user_stories.py::test_kiosk_token_is_revocable`, `::test_us01_ac1_roster_exposes_nothing...` |
| NFR-S-07 | Process — dependency scanning and an annual penetration test | — |
| NFR-U-01 | Clock-in and clock-out are one interaction on the tracker and two at the kiosk | Manual |
| NFR-U-02 | Semantic HTML, labelled controls, visible focus rings, ≥ 4.5:1 contrast, live regions | Manual audit before launch |
| NFR-U-03 | Kiosk targets ≥ 76 px, 2 rem type, high-contrast palette | `static/css/kiosk.css` |
| NFR-U-04 | Formats, week start and duration display are configuration; the UI ships in English with the string surface isolated for Slovak | — |
| NFR-U-05 | Responsive from 320 px, no horizontal scroll on core flows | Manual |
| NFR-M-01 | Every policy is editable in the administration screens | `test_workflows.py` (policy, rule-parameter and settings tests) |
| NFR-M-02 | Structured logging; no personal data beyond a pseudonymous id | `services/notifications.py`, `services/webhooks.py` |
| NFR-M-03 | 253 tests; 91 % branch coverage of the calculation and rules engine; `test_calc.py` is the worked-example suite for HR/Payroll sign-off | `coverage report` |
| NFR-M-04 | Environment-driven configuration supports separate environments | `app/config.py` |

---

## Known gaps and deliberate limits

These are stated plainly rather than left to be discovered.

1. **E-mail and push delivery adapters are stubs.** The notification and
   scheduled-report framework decides *what* to send, to *whom*, on *which*
   channel, and records it; wiring an SMTP or push provider is a
   ten-line adapter in `services/notifications.py`. Nothing else changes.
2. **SSO assertion consumption is not implemented.** The service-provider
   metadata and the SCIM read surface are there; the assertion/callback
   handlers depend on the chosen identity provider and are a Phase 3
   integration task.
3. **Photo capture at the kiosk (FR-D-11, a *Could*)** exists as a switch and a
   warning only. Capturing and storing the image is withheld until the DPIA and
   works-council position exist, which is what DP-02 and DP-12 require.
4. **Performance targets are not yet measured.** The design decisions that
   serve them are in place (materialised aggregates, independent kiosk writes,
   a separable batch tier), but NFR-P-01 to NFR-P-04 need a load test against
   production-like hardware before launch.
5. **`AuditRecord` immutability is enforced in the application.** The
   database-level `DENY UPDATE/DELETE` grant described in the README is what
   makes FR-L-02 true in the strong sense; it is a deployment step.
6. **The mobile application is a responsive web client**, not a native app.
   The API and the capture-channel flag for mobile are in place.
