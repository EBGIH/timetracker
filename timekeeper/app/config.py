"""Application configuration.

Everything that a labour-law jurisdiction or a collective agreement might
change is a parameter, never a constant (spec section 16).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- Persistence -----------------------------------------------------
    # SQLite by default so the system runs with no infrastructure.
    # Point DATABASE_URL at PostgreSQL for a real deployment.
    database_url: str = _env("DATABASE_URL", "sqlite:///./timekeeper.db")

    # --- Security --------------------------------------------------------
    secret_key: str = _env("TK_SECRET_KEY", "dev-only-secret-change-me")
    access_token_minutes: int = int(_env("TK_TOKEN_MINUTES", "720"))
    pbkdf2_iterations: int = int(_env("TK_PBKDF2_ITERATIONS", "210000"))

    # Authentication hardening (NFR-S-05, US-01 AC-3)
    login_max_attempts: int = int(_env("TK_LOGIN_MAX_ATTEMPTS", "10"))
    login_lockout_seconds: int = int(_env("TK_LOGIN_LOCKOUT_SECONDS", "300"))
    kiosk_max_attempts: int = int(_env("TK_KIOSK_MAX_ATTEMPTS", "5"))
    kiosk_lockout_seconds: int = int(_env("TK_KIOSK_LOCKOUT_SECONDS", "300"))

    # --- Operations ------------------------------------------------------
    # Nightly batch for rolling-window rules (WT-01, WT-05) and retention.
    batch_interval_seconds: int = int(_env("TK_BATCH_INTERVAL_SECONDS", "3600"))
    enable_scheduler: bool = _env("TK_ENABLE_SCHEDULER", "1") == "1"

    app_base_url: str = _env("TK_BASE_URL", "http://localhost:8000")


settings = Settings()

# Duration displayed either as h:mm or as decimal hours (FR-A-04).
DURATION_FORMATS = ("hm", "decimal")
PERIOD_TYPES = ("weekly", "biweekly", "semimonthly", "monthly")
CAPTURE_CHANNELS = ("timer", "manual", "grid", "kiosk", "mobile", "api")
ROLES = ("owner", "admin", "hr", "manager", "employee", "limited")
