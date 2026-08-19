"""Outbound webhooks (FR-J-06).

Deliveries are persisted first and dispatched by the background worker, so an
unreachable endpoint can never block a clock-in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Webhook, WebhookDelivery, new_id

log = logging.getLogger("timekeeper.webhook")

EVENTS = (
    "clock_in",
    "clock_out",
    "break_start",
    "break_end",
    "period_submitted",
    "period_approved",
    "period_locked",
    "exception_raised",
    "absence_approved",
    "payroll_exported",
)


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def emit(db: Session, org_id: str, event: str, payload: dict) -> None:
    hooks = db.scalars(
        select(Webhook).where(Webhook.org_id == org_id, Webhook.active.is_(True))
    ).all()
    for hook in hooks:
        if hook.events and event not in hook.events:
            continue
        body = json.dumps({"event": event, "data": payload}, sort_keys=True, default=str)
        db.add(
            WebhookDelivery(
                id=new_id(),
                webhook_id=hook.id,
                event=event,
                payload={"event": event, "data": payload},
                signature=sign(hook.secret, body),
                status="queued",
            )
        )


def dispatch_pending(db: Session, limit: int = 50) -> int:
    """Called by the background worker. Uses httpx when available."""
    pending = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status.in_(("queued", "retry")))
        .limit(limit)
    ).all()
    if not pending:
        return 0
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return 0
    sent = 0
    with httpx.Client(timeout=5.0) as client:
        for delivery in pending:
            hook = db.get(Webhook, delivery.webhook_id)
            if hook is None or not hook.active:
                delivery.status = "cancelled"
                continue
            delivery.attempts += 1
            try:
                response = client.post(
                    hook.url,
                    json=delivery.payload,
                    headers={"X-TimeKeeper-Signature": delivery.signature},
                )
                delivery.response_code = response.status_code
                delivery.status = "sent" if response.is_success else "retry"
                sent += 1 if response.is_success else 0
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("webhook delivery failed: %s", exc)
                delivery.status = "failed" if delivery.attempts >= 5 else "retry"
    db.commit()
    return sent
