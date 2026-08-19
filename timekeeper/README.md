# TimeKeeper

An employee attendance and time-tracking system, built to the *Business
Requirements & Functional Specification v1.0* (Employee Attendance & Time
Tracking System, 19 August 2026).

All three delivery phases of section 19 are implemented: the MVP, absence and
workflow depth, and the integration/scale phase.

---

## Running it

```bash
pip install -r requirements.txt

python seed_demo.py --reset          # optional: a worked demonstration workspace
uvicorn app.main:app --reload
```

Then open <http://localhost:8000>.

With no database present the application starts empty and the first screen is
the one-time setup form that creates the organisation and its owner. With
`seed_demo.py` you get a plant and an office, five roles, six weeks of
attendance, absence, exceptions and a working kiosk:

| Role | Sign in as | Password |
|---|---|---|
| Owner | `owner@nordvest.example` | `TimeKeeper2026!` |
| HR administrator | `hr@nordvest.example` | `TimeKeeper2026!` |
| Payroll | `payroll@nordvest.example` | `TimeKeeper2026!` |
| Line manager | `manager@nordvest.example` | `TimeKeeper2026!` |
| Employee | `lucia@nordvest.example` | `TimeKeeper2026!` |

The kiosk is at `/kiosk.html?token=demo-kiosk-token`; the seed script prints
the PINs of the five shift workers.

### Tests

```bash
python -m pytest                                     # 250 tests
python -m coverage run --branch -m pytest && python -m coverage report
```

The calculation and rules engine is covered to 91 % of branches, which meets
NFR-M-03. `tests/test_calc.py` is written as the worked-example regression
suite the specification asks HR and Payroll to sign off: each test states the
example in plain language before asserting it.

### Configuration

Everything is environment-driven; the defaults run on SQLite with no
infrastructure.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./timekeeper.db` | Point at PostgreSQL for a real deployment |
| `TK_SECRET_KEY` | *dev value* | **Must** be set in production — signs session tokens |
| `TK_TOKEN_MINUTES` | `720` | Session lifetime |
| `TK_PBKDF2_ITERATIONS` | `210000` | Password and PIN KDF cost |
| `TK_KIOSK_MAX_ATTEMPTS` / `TK_KIOSK_LOCKOUT_SECONDS` | `5` / `300` | Kiosk PIN lock-out (US-01 AC-3) |
| `TK_ENABLE_SCHEDULER` | `1` | In-process nightly batch; switch off and drive `POST /api/admin/run-batch` externally when running more than one instance |
| `TK_BATCH_INTERVAL_SECONDS` | `3600` | How often that batch runs |
| `TK_BASE_URL` | `http://localhost:8000` | Used in notification links and SSO metadata |

---

## What it does

### Capture (Modules C, D, E)

* A **live timer** whose state is server-side, so it survives a page reload, a
  browser close and a change of device.
* **Manual entry** and a **weekly timesheet grid** (cost centres as rows, days
  as columns) for people who fill the week in retrospectively.
* A **kiosk** for shift staff with no company e-mail: a revocable full-screen
  link, a 4- or 6-digit PIN or QR code, a roster showing who is currently in,
  break start/stop, and an offline queue that replays events with their
  original timestamps and idempotency keys when the network returns.
* **Breaks** typed as paid or unpaid, with optional automatic deduction.
* Overlapping sessions are impossible: an attempt returns the conflicting
  entry and the ways to resolve it.
* Sessions running past a configurable maximum are flagged, the employee is
  told, and — where the organisation enables it — auto-stopped at the shift
  end and marked as needing confirmation.

### Calculation (section 13)

Net worked time is presence minus unpaid breaks; paid breaks count as worked
time. Overtime is computed on net time only, and split into four **mutually
exclusive** categories with the priority *public holiday → weekend → night →
standard*, so payroll can never multiply the same minute twice. Night hours
are additionally reported in their own field. Rounding, where switched on, is
applied at clock-in and clock-out only, never to computed totals, and in the
same direction for both events.

### Compliance (section 16)

WT-01 to WT-07 are implemented as **parameters, not constants**, because
national law is frequently stricter than the Directive. Parameters are
versioned: an evaluation of a historic day uses the values that were in force
on that day. Rules run on write for same-day feedback and in a nightly batch
for rolling windows. Individual opt-outs from the 48-hour average are recorded
per employee with an effective date and a reference to the signed agreement.
The compliance report is the evidence artefact for a labour inspection and is
exportable for any historic period.

### Absence, approval and payroll (Modules F, G, H, J)

Absence policies with accrual, carry-over, notice periods, approver chains,
document requirements and minimum team coverage. Balance is consumed at
approval, not at request, and planned absence is shown separately from taken.

Periods are submitted, reviewed, approved in bulk, and locked. A locked period
is immutable: changes go through a correction request that creates a new
version and leaves the original in place. The payroll export is a configurable
delimited layout with a SHA-256 checksum, a full history, byte-for-byte
re-download, and a reconciliation report showing what changed since the
previous run.

### Reporting (Module I)

Live team board, attendance, weekly, summary (grouped by employee, team, site,
date or week), detailed, absence, overtime, compliance and exception queue —
each exportable to CSV, XLSX and PDF, saveable as a named filter set,
schedulable by cron, and shareable through an expiring link that runs with the
sharer's visibility and never wider.

### Governance (Module L, section 15)

Every mutation is written to an append-only audit log with actor, UTC
timestamp, action, entity and the before/after values, with credential
material redacted. Access to another employee's detail is itself logged.
Employees have in-product access to a plain-language privacy notice and a
one-click export of everything held about them. Retention is enforced by a job
that deletes beyond the statutory minimum and logs the deletion.

The features the specification rejects on proportionality grounds — screenshot
capture, application and website monitoring, continuous location tracking,
biometric identification — are **not implemented**, and the privacy notice
says so. Geolocation, where a site geofence is configured, stores only the
boolean "inside the fence" result and the site identifier.

---

## How it is put together

```
app/
  main.py               FastAPI application, first-run setup, batch scheduler
  config.py             Environment-driven settings
  database.py           Engine and session
  models.py             The physical model of specification section 12
  schemas.py            Request/response contracts
  security.py           Tokens, PBKDF2 hashing, the section 9.1 role matrix
  audit.py              Append-only audit writer with credential redaction
  services/
    timeutil.py         UTC/local conversion, interval algebra, rounding, periods
    calc.py             The calculation engine — BR-01 .. BR-12
    rules.py            Working-time rules WT-01 .. WT-07 and exceptions
    absence.py          Entitlement, accrual, validation, approver chains
    periods.py          Period resolution, submission state, locking
    payroll.py          Export rows, layouts, checksums, reconciliation
    reports.py          The nine-report catalogue of section 17
    exports.py          CSV / XLSX / PDF writers
    batch.py            Nightly jobs: runaway sessions, reminders, retention
    notifications.py    Module K catalogue and delivery
    webhooks.py         Signed outbound events
    holidays.py         Public-holiday generation
    totp.py             RFC 6238, on the standard library
  routers/              One module per functional area
  static/               The web client: SPA, kiosk, invitation, shared report
tests/                  250 tests, including the worked-example suite
docs/TRACEABILITY.md    Every requirement ID mapped to code and test
```

**Design choices worth knowing about.**

*Aggregates are materialised.* `DayAggregate` is recomputed on every write that
touches a day and by the nightly batch. Reports read aggregates, not raw
sessions, which is what keeps the 500-employee monthly report inside the
five-second target of NFR-P-03.

*Time is stored in UTC, interpreted locally.* Every timestamp is naive UTC. The
local day is derived from the site's time zone, falling back to the
organisation's, so an overnight shift is attributed correctly to each calendar
day it touches and a time-zone change does not rewrite history.

*Nothing is overwritten.* Sessions and time entries carry a version and a
`superseded_by` pointer. Approving a correction writes a new row and links the
old one; the audit log holds both values either way.

*Authorisation is server-side, always.* The client hides what a role cannot
use, but every endpoint re-derives the scope from the role matrix. The tests in
`tests/test_permissions.py` assert the matrix of section 9.1 cell by cell and
then prove the enforcement over HTTP.

---

## Deploying

The defaults are for development. For production:

1. Set `TK_SECRET_KEY` to a long random value and `DATABASE_URL` to PostgreSQL.
2. Terminate TLS in front of the application (NFR-S-01) and enable encryption
   at rest on the database and its backups.
3. Run more than one application instance behind a load balancer, set
   `TK_ENABLE_SCHEDULER=0`, and drive `POST /api/admin/run-batch` from a single
   external scheduler so the nightly job runs once.
4. Grant the application's database role `SELECT` and `INSERT` on
   `audit_record` but **not** `UPDATE` or `DELETE`, so FR-L-02 is enforced by
   the database and not only by the application.
5. Host inside the EU/EEA (DP-10) and complete the DPIA before launch (DP-02).

An OpenAPI description is served at `/docs`.

---

## What is deliberately not here

Per section 5.2 and Appendix A, and left out on purpose:

* Gross-to-net payroll calculation — this system produces the inputs.
* Client invoicing, billable rates, project budgets and profitability.
* Screenshot capture, application/website monitoring, idle detection.
* Biometric identification.
* Continuous GPS tracking.
* Recruitment, performance management, expenses.

Photo capture at a kiosk (FR-D-11, a *Could*) exists as a per-kiosk switch and
is off by default; the administration screen states that switching it on
requires a documented legal basis, works-council agreement and a repeat of the
DPIA.

The open questions of section 22 are all expressed as configuration rather than
assumptions — period type and cut-off (Q-2), rounding and its direction (Q-3),
whether overtime needs prior approval (Q-4), opt-outs (Q-5), geofencing per
site (Q-6), retention (Q-7) — so answering them is a settings change, not a
code change.
