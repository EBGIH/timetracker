"""TimeKeeper — employee attendance and time tracking.

Implements the Business Requirements & Functional Specification v1.0
(Employee Attendance & Time Tracking System).
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import settings
from .database import SessionLocal, get_db, init_db
from .models import Credential, Organisation, User, WorkingPattern, new_id
from .routers import (
    absence,
    approvals,
    attendance,
    auth,
    integrations,
    kiosk,
    misc,
    org,
    payroll,
    reports,
    users,
)
from .security import Principal, get_principal, hash_secret
from .services import batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("timekeeper")

STATIC_DIR = Path(__file__).parent / "static"

_stop_event = threading.Event()


def _scheduler_loop() -> None:  # pragma: no cover - background thread
    """Runs the nightly batch on an interval. In a multi-instance deployment
    replace this with an external scheduler calling POST /api/admin/run-batch."""
    while not _stop_event.wait(settings.batch_interval_seconds):
        db = SessionLocal()
        try:
            result = batch.run_all(db)
            log.info("batch complete: %s", result)
        except Exception:
            log.exception("batch run failed")
            db.rollback()
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    thread = None
    if settings.enable_scheduler:
        thread = threading.Thread(target=_scheduler_loop, name="tk-batch", daemon=True)
        thread.start()
    yield
    _stop_event.set()
    if thread:
        thread.join(timeout=2)


app = FastAPI(
    title="TimeKeeper",
    version="1.0.0",
    description=(
        "Employee attendance and time tracking.\n\n"
        "Authentication: send `Authorization: Bearer <token>` with either a "
        "session token from `POST /api/auth/login` or an API key issued at "
        "`POST /api/integrations/api-keys`. Authorisation is enforced "
        "server-side on every request against the role matrix in section 9.1 "
        "of the specification."
    ),
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(org.router)
app.include_router(users.router)
app.include_router(attendance.router)
app.include_router(kiosk.router)
app.include_router(absence.router)
app.include_router(approvals.router)
app.include_router(reports.router)
app.include_router(reports.shared)
app.include_router(payroll.router)
app.include_router(integrations.router)
app.include_router(integrations.public_api)
app.include_router(misc.dashboard)
app.include_router(misc.notifications_router)
app.include_router(misc.audit_router)
app.include_router(misc.privacy_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    )
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/setup/status")
def setup_status(db: Session = Depends(get_db)):
    return {"initialised": db.scalar(select(Organisation.id)) is not None}


@app.post("/api/setup")
def first_run_setup(body: dict, request: Request, db: Session = Depends(get_db)):
    """First-run bootstrap: creates the organisation and its owner. Refuses
    once an organisation exists."""
    if db.scalar(select(Organisation.id)) is not None:
        raise HTTPException(409, "This installation is already initialised")
    required = ("organisation", "first_name", "last_name", "email", "password")
    missing = [field for field in required if not body.get(field)]
    if missing:
        raise HTTPException(400, f"Missing: {', '.join(missing)}")
    if len(body["password"]) < 10:
        raise HTTPException(400, "The password must be at least 10 characters")

    organisation = Organisation(
        id=new_id(), name=body["organisation"],
        country=body.get("country", "SK"),
        timezone=body.get("timezone", "Europe/Bratislava"),
    )
    db.add(organisation)
    db.flush()
    owner = User(
        id=new_id(), org_id=organisation.id, personnel_number="0001",
        first_name=body["first_name"], last_name=body["last_name"],
        email=body["email"].lower().strip(), role="owner",
        employment_start=date.today(),
    )
    db.add(owner)
    db.flush()
    hashed, salt = hash_secret(body["password"])
    db.add(Credential(user_id=owner.id, type="password", hash=hashed, salt=salt))
    db.add(
        WorkingPattern(
            id=new_id(), user_id=owner.id, valid_from=owner.employment_start,
            contracted_hours_per_week=40.0,
            expected_minutes=[480, 480, 480, 480, 480, 0, 0],
        )
    )
    audit.record(
        db, action="setup.completed", entity_type="organisation",
        entity_id=organisation.id, org_id=organisation.id, actor_user_id=owner.id,
        after={"organisation": organisation.name},
    )
    db.commit()
    return {"organisation_id": organisation.id, "owner_id": owner.id,
            "next": "Sign in, then configure locations, teams and policies."}


@app.post("/api/admin/run-batch")
def run_batch(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    """Manual trigger for the nightly batch — used in testing and by an
    external scheduler in a multi-instance deployment."""
    principal.require("configure_org")
    return batch.run_all(db)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, str):
        detail = {"message": detail}
    return JSONResponse(status_code=exc.status_code, content=detail,
                        headers=getattr(exc, "headers", None))


# --- Static client ---------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/kiosk.html")
    def kiosk_page():
        return FileResponse(STATIC_DIR / "kiosk.html")

    @app.get("/invite/{token}")
    def invite_page(token: str):
        return FileResponse(STATIC_DIR / "invite.html")

    @app.get("/shared/{token}")
    def shared_page(token: str):
        return FileResponse(STATIC_DIR / "shared.html")
