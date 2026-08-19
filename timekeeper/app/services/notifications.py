"""Notification framework (Module K).

FR-K-04: the payload carries no more personal data than necessary and links
back to the system rather than embedding the data. Delivery adapters for
e-mail and push are pluggable; the default adapter writes to the outbox table
and the application log so the system runs without an SMTP dependency.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Notification, User, new_id

log = logging.getLogger("timekeeper.notify")

# type -> (default channels, may the user switch it off?)
CATALOGUE: dict[str, tuple[tuple[str, ...], bool]] = {
    # Employee (FR-K-01)
    "timer_runaway": (("inapp", "email"), False),
    "period_due": (("inapp", "email"), False),
    "entry_amended": (("inapp", "email"), False),
    "absence_decided": (("inapp", "email"), False),
    "period_decided": (("inapp", "email"), False),
    # Manager (FR-K-02)
    "timesheet_awaiting": (("inapp", "email"), True),
    "absence_awaiting": (("inapp", "email"), False),
    "exception_raised": (("inapp",), True),
    "absent_without_notice": (("inapp", "email"), True),
    # Housekeeping
    "report_scheduled": (("email",), True),
    "correction_awaiting": (("inapp", "email"), False),
    "overtime_awaiting": (("inapp",), True),
}

MANDATORY = {name for name, (_c, optional) in CATALOGUE.items() if not optional}


def channels_for(user: User, type_: str) -> list[str]:
    defaults, optional = CATALOGUE.get(type_, (("inapp",), True))
    prefs = (user.notification_prefs or {}).get(type_)
    if prefs is None or not optional:
        return list(defaults)
    if prefs is False:
        return ["inapp"] if "inapp" in defaults else []
    if isinstance(prefs, list):
        # FR-K-03: a user may narrow channels but not below the mandatory set.
        chosen = [c for c in prefs if c in defaults]
        return chosen or list(defaults)
    return list(defaults)


def notify(
    db: Session,
    user_id: str,
    type_: str,
    title: str,
    body: str = "",
    link: str = "",
) -> list[Notification]:
    user = db.get(User, user_id)
    if user is None or user.status != "active":
        return []
    if not user.has_login:
        # A limited member has no inbox; kiosk feedback is immediate instead.
        return []
    created: list[Notification] = []
    for channel in channels_for(user, type_):
        note = Notification(
            id=new_id(),
            org_id=user.org_id,
            user_id=user_id,
            type=type_,
            channel=channel,
            title=title[:200],
            body=body[:500],
            link=(link or settings.app_base_url)[:300],
        )
        db.add(note)
        created.append(note)
        if channel != "inapp":
            log.info(
                "notify channel=%s type=%s user=%s title=%s", channel, type_, user_id, title
            )
    return created


def notify_many(db: Session, user_ids, type_: str, title: str, body: str = "", link: str = ""):
    for user_id in set(user_ids):
        notify(db, user_id, type_, title, body, link)


def unread_count(db: Session, user_id: str) -> int:
    return len(
        db.scalars(
            select(Notification.id).where(
                Notification.user_id == user_id,
                Notification.channel == "inapp",
                Notification.read_at.is_(None),
            )
        ).all()
    )
