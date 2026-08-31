"""AgentMail Draft API adapter and database-backed idempotent outbox."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import DeliveryState
from .db import DeliveryModel, WebhookEventModel, utcnow

LOGGER = logging.getLogger(__name__)


class DraftClient(Protocol):
    def create_draft(
        self,
        *,
        client_id: str,
        to: list[str],
        subject: str,
        text: str,
        html: str,
        send_at: datetime | None,
        labels: list[str],
    ) -> str: ...

    def send_draft(self, draft_id: str) -> str: ...

    def get_draft(self, draft_id: str) -> dict[str, Any]: ...

    def update_draft(
        self,
        draft_id: str,
        *,
        to: list[str],
        subject: str,
        text: str,
        html: str,
        send_at: datetime | None,
    ) -> str: ...

    def find_draft_by_label(
        self, label: str, *, after: datetime | None = None
    ) -> dict[str, Any] | None: ...

    def find_message_by_label(
        self, label: str, *, after: datetime | None = None
    ) -> dict[str, Any] | None: ...


class AgentMailClient:
    """Thin lazy wrapper around the official AgentMail Python SDK."""

    def __init__(self, *, api_key: str, inbox_id: str) -> None:
        from agentmail import AgentMail

        self.client = AgentMail(api_key=api_key)
        self.inbox_id = inbox_id

    def create_draft(
        self,
        *,
        client_id: str,
        to: list[str],
        subject: str,
        text: str,
        html: str,
        send_at: datetime | None,
        labels: list[str],
    ) -> str:
        kwargs: dict[str, Any] = {
            "inbox_id": self.inbox_id,
            "to": to,
            "subject": subject,
            "text": text,
            "html": html,
            "client_id": client_id,
            "labels": labels,
        }
        if send_at is not None:
            kwargs["send_at"] = send_at.astimezone(UTC)
        draft = self._retry(lambda: self.client.inboxes.drafts.create(**kwargs), idempotent=True)
        return str(draft.draft_id)

    def send_draft(self, draft_id: str) -> str:
        message = self._retry(
            lambda: self.client.inboxes.drafts.send(
                inbox_id=self.inbox_id, draft_id=draft_id
            ),
            idempotent=False,
        )
        return str(message.message_id)

    def update_draft(
        self,
        draft_id: str,
        *,
        to: list[str],
        subject: str,
        text: str,
        html: str,
        send_at: datetime | None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "inbox_id": self.inbox_id,
            "draft_id": draft_id,
            "to": to,
            "subject": subject,
            "text": text,
            "html": html,
        }
        if send_at is not None:
            kwargs["send_at"] = send_at.astimezone(UTC)
        draft = self._retry(lambda: self.client.inboxes.drafts.update(**kwargs), idempotent=True)
        return str(draft.draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self._retry(
            lambda: self.client.inboxes.drafts.get(
                inbox_id=self.inbox_id, draft_id=draft_id
            ),
            idempotent=True,
        )
        return _sdk_object(draft)

    def find_draft_by_label(
        self, label: str, *, after: datetime | None = None
    ) -> dict[str, Any] | None:
        result = self._retry(
            lambda: self.client.inboxes.drafts.list(
                inbox_id=self.inbox_id,
                labels=[label],
                after=after,
                limit=10,
            ),
            idempotent=True,
        )
        drafts = getattr(result, "drafts", None) or []
        return _sdk_object(drafts[0]) if drafts else None

    def find_message_by_label(
        self, label: str, *, after: datetime | None = None
    ) -> dict[str, Any] | None:
        result = self._retry(
            lambda: self.client.inboxes.messages.list(
                inbox_id=self.inbox_id,
                labels=[label],
                after=after,
                limit=10,
            ),
            idempotent=True,
        )
        messages = getattr(result, "messages", None) or []
        return _sdk_object(messages[0]) if messages else None

    @staticmethod
    def _retry(call, *, idempotent: bool, max_attempts: int = 4):
        """Honor Retry-After; retry ambiguous failures only for safe operations."""

        for attempt in range(1, max_attempts + 1):
            try:
                return call()
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                retryable = status == 429 or (
                    idempotent
                    and (
                        isinstance(exc, (TimeoutError, ConnectionError, httpx.HTTPError))
                        or isinstance(status, int)
                        and status >= 500
                    )
                )
                if not retryable or attempt == max_attempts:
                    raise
                headers = getattr(exc, "headers", None) or {}
                raw_retry_after = headers.get("retry-after") or headers.get("Retry-After")
                try:
                    delay = float(raw_retry_after) if raw_retry_after is not None else 2 ** (attempt - 1)
                except (TypeError, ValueError):
                    delay = 2 ** (attempt - 1)
                time.sleep(delay + random.uniform(0, 0.25))
        raise RuntimeError("unreachable")


def deliver_outbox(
    session: Session,
    *,
    mode: str,
    recipient: str | None = None,
    client: DraftClient | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Create/send drafts once; ambiguous failures become ``unknown`` and are never retried blindly."""

    now = now or utcnow()
    rows = session.scalars(
        select(DeliveryModel).where(DeliveryModel.state == DeliveryState.PENDING.value)
    ).all()
    result = {"shadow": 0, "scheduled": 0, "sent": 0, "unknown": 0, "failed": 0}
    for row in rows:
        message = dict(row.metadata_json or {})
        label = str(message.get("agentmail_label") or _delivery_label(row.delivery_key))
        message["agentmail_label"] = label
        row.metadata_json = message
        if mode != "live":
            row.state = DeliveryState.DRAFT.value
            message["shadow"] = True
            message["agentmail_send_mode"] = "shadow"
            row.metadata_json = message
            if client is not None and recipient:
                try:
                    if row.agentmail_draft_id:
                        row.agentmail_draft_id = client.update_draft(
                            row.agentmail_draft_id,
                            to=[recipient],
                            subject=str(message.get("subject", "AI Research Radar")),
                            text=str(message.get("text", "")),
                            html=str(message.get("html", "")),
                            send_at=None,
                        )
                    else:
                        row.agentmail_draft_id = client.create_draft(
                            client_id=_draft_client_id(row.delivery_key),
                            to=[recipient],
                            subject=str(message.get("subject", "AI Research Radar")),
                            text=str(message.get("text", "")),
                            html=str(message.get("html", "")),
                            send_at=None,
                            labels=[label],
                        )
                except (TimeoutError, ConnectionError, httpx.TimeoutException) as exc:
                    LOGGER.warning("AgentMail shadow draft timeout for %s: %s", row.delivery_key, exc)
                    row.last_error = f"shadow draft timeout: {exc}"[:2000]
                except Exception as exc:
                    LOGGER.warning("AgentMail shadow draft creation failed for %s: %s", row.delivery_key, exc)
                    row.last_error = f"shadow draft failed: {exc}"[:2000]
            row.updated_at = utcnow()
            session.flush()
            result["shadow"] += 1
            continue
        if client is None:
            raise ValueError("live delivery requires AgentMail credentials")
        if not recipient:
            raise ValueError("live delivery requires DIGEST_RECIPIENT")
        row.attempt_count += 1
        scheduled_at = _aware_utc(row.send_at)
        subject = str(message.get("subject", "AI Research Radar"))
        if row.delivery_kind == "digest" and scheduled_at and scheduled_at <= now:
            subject = f"[延迟日报] {subject}"
        stage = "draft"
        try:
            target_send_at = scheduled_at if scheduled_at and scheduled_at > now else None
            message["agentmail_send_mode"] = (
                "scheduled" if target_send_at is not None else "immediate"
            )
            row.metadata_json = message
            if row.agentmail_draft_id:
                draft_id = client.update_draft(
                    row.agentmail_draft_id,
                    to=[recipient],
                    subject=subject,
                    text=str(message.get("text", "")),
                    html=str(message.get("html", "")),
                    send_at=target_send_at,
                )
            else:
                draft_id = client.create_draft(
                    client_id=_draft_client_id(row.delivery_key),
                    to=[recipient],
                    subject=subject,
                    text=str(message.get("text", "")),
                    html=str(message.get("html", "")),
                    send_at=target_send_at,
                    labels=[label],
                )
            row.agentmail_draft_id = draft_id
            if scheduled_at and scheduled_at > now:
                row.state = DeliveryState.SCHEDULED.value
                result["scheduled"] += 1
            else:
                # A draft create is idempotent; draft send itself is not. Any timeout is unknown.
                row.state = DeliveryState.SENDING.value
                session.flush()
                # Persist the claimed Draft and sending state before the
                # non-idempotent external send. A crash can then only enter
                # reconciliation; it cannot return this row to pending.
                session.commit()
                stage = "send"
                row.agentmail_message_id = client.send_draft(draft_id)
                row.state = DeliveryState.SENT.value
                result["sent"] += 1
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            row.state = DeliveryState.UNKNOWN.value
            row.last_error = "ambiguous AgentMail timeout; reconcile before any retry"
            result["unknown"] += 1
        except Exception as exc:  # SDK-specific HTTP exception types vary by release.
            if stage == "draft" and _retryable_idempotent_error(exc):
                # create/update uses deterministic client_id/Draft ID. Keep it
                # retryable while still failing this workflow visibly.
                row.state = DeliveryState.PENDING.value
            elif stage == "send" and _retryable_idempotent_error(exc):
                # A server failure after the non-idempotent send call is
                # ambiguous; only reconciliation may advance it.
                row.state = DeliveryState.UNKNOWN.value
                result["unknown"] += 1
                row.last_error = str(exc)[:2000]
                row.updated_at = utcnow()
                continue
            else:
                row.state = DeliveryState.FAILED.value
            row.last_error = str(exc)[:2000]
            result["failed"] += 1
        row.updated_at = utcnow()
    session.flush()
    return result


def _retryable_idempotent_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return bool(
        status == 429
        or isinstance(status, int)
        and status >= 500
        or isinstance(exc, (TimeoutError, ConnectionError, httpx.HTTPError))
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def reconcile_drafts(session: Session, client: DraftClient | None = None) -> dict[str, int]:
    rows = session.scalars(
        select(DeliveryModel).where(
            DeliveryModel.state.in_(
                [DeliveryState.SCHEDULED.value, DeliveryState.SENDING.value, DeliveryState.UNKNOWN.value]
            )
        )
    ).all()
    result = {
        "checked": 0,
        "draft": 0,
        "scheduled": 0,
        "sent": 0,
        "delivered": 0,
        "failed": 0,
        "unknown": 0,
    }
    if client is None:
        return result
    for row in rows:
        metadata = dict(row.metadata_json or {})
        label = str(metadata.get("agentmail_label") or _delivery_label(row.delivery_key))
        metadata["agentmail_label"] = label
        row.metadata_json = metadata
        after = _aware_utc(row.created_at)
        if not row.agentmail_draft_id:
            try:
                found = client.find_draft_by_label(label, after=after)
            except Exception:
                found = None
            if found:
                row.agentmail_draft_id = str(found.get("draft_id") or "") or None
        result["checked"] += 1
        draft_error: Exception | None = None
        draft = None
        if row.agentmail_draft_id:
            try:
                draft = client.get_draft(row.agentmail_draft_id)
            except Exception as exc:
                draft_error = exc
        if draft is not None:
            raw_status = draft.get("send_status")
            status = str(raw_status) if raw_status is not None else ""
            send_mode = str(metadata.get("agentmail_send_mode") or "")
            if not send_mode:
                scheduled_at = _aware_utc(row.send_at)
                send_mode = (
                    "immediate"
                    if scheduled_at is None or scheduled_at <= utcnow()
                    else "scheduled"
                )
            if metadata.get("shadow"):
                row.state = DeliveryState.DRAFT.value
                result["draft"] += 1
            elif status == "failed":
                row.state = DeliveryState.FAILED.value
                result["failed"] += 1
            elif not status and send_mode == "immediate":
                # AgentMail consumes a Draft after a successful send. If an
                # immediate send timed out but that exact Draft still exists,
                # retrying the same Draft ID cannot create a second Draft.
                row.state = DeliveryState.SENDING.value
                session.flush()
                session.commit()
                try:
                    row.agentmail_message_id = client.send_draft(
                        str(row.agentmail_draft_id)
                    )
                    row.state = DeliveryState.SENT.value
                    applied = _apply_recorded_webhooks(session, row)
                    if applied == DeliveryState.DELIVERED.value:
                        result["delivered"] += 1
                    else:
                        result["sent"] += 1
                except (TimeoutError, ConnectionError, httpx.TimeoutException):
                    row.state = DeliveryState.UNKNOWN.value
                    row.last_error = "ambiguous retry timeout; reconcile the same Draft again"
                    result["unknown"] += 1
                except Exception as exc:
                    if _retryable_idempotent_error(exc):
                        row.state = DeliveryState.UNKNOWN.value
                        row.last_error = (
                            "ambiguous retry provider failure; reconcile the same Draft again: "
                            f"{exc}"
                        )[:2000]
                        result["unknown"] += 1
                    else:
                        row.state = DeliveryState.FAILED.value
                        row.last_error = str(exc)[:2000]
                        result["failed"] += 1
            else:
                row.state = (
                    DeliveryState.SENDING.value
                    if status == "sending"
                    else DeliveryState.SCHEDULED.value
                )
                result["scheduled"] += 1
        else:
            try:
                sent = client.find_message_by_label(label, after=after)
            except Exception as exc:
                sent = None
                draft_error = draft_error or exc
            if sent and sent.get("message_id"):
                row.agentmail_message_id = str(sent["message_id"])
                row.state = DeliveryState.SENT.value
                applied = _apply_recorded_webhooks(session, row)
                if applied == DeliveryState.DELIVERED.value:
                    result["delivered"] += 1
                else:
                    result["sent"] += 1
            else:
                row.state = DeliveryState.UNKNOWN.value
                row.last_error = (
                    f"Draft/message not found during reconcile: {draft_error}"
                    if draft_error
                    else "Draft/message not found during reconcile"
                )[:2000]
                result["unknown"] += 1
        row.updated_at = utcnow()
    session.flush()
    return result


WEBHOOK_STATES = {
    "message.sent": DeliveryState.SENT.value,
    "message.delivered": DeliveryState.DELIVERED.value,
    "message.bounced": DeliveryState.BOUNCED.value,
    "message.rejected": DeliveryState.REJECTED.value,
    "message.complained": DeliveryState.COMPLAINED.value,
}

STATE_RANK = {
    DeliveryState.SENT.value: 30,
    DeliveryState.DELIVERED.value: 40,
    DeliveryState.BOUNCED.value: 50,
    DeliveryState.REJECTED.value: 50,
    DeliveryState.COMPLAINED.value: 60,
}


def _apply_recorded_webhooks(session: Session, row: DeliveryModel) -> str:
    """Attach early webhooks after a scheduled Draft becomes a Message."""

    if not row.agentmail_message_id:
        return row.state
    events = session.scalars(
        select(WebhookEventModel).where(
            WebhookEventModel.message_id == row.agentmail_message_id,
            WebhookEventModel.signature_verified.is_(True),
        )
    ).all()
    chosen = row.state
    chosen_rank = STATE_RANK.get(chosen, 0)
    for event in events:
        state = WEBHOOK_STATES.get(event.event_type)
        if state and STATE_RANK[state] >= chosen_rank:
            chosen = state
            chosen_rank = STATE_RANK[state]
        event.delivery_key = row.delivery_key
        event.processed_at = event.processed_at or utcnow()
    row.state = chosen
    if chosen == DeliveryState.DELIVERED.value and row.delivered_at is None:
        row.delivered_at = utcnow()
    return chosen


def _delivery_label(delivery_key: str) -> str:
    digest = hashlib.sha256(delivery_key.encode("utf-8")).hexdigest()[:24]
    return f"radar-delivery-{digest}"


def _draft_client_id(delivery_key: str) -> str:
    """Map an internal delivery key to AgentMail's restricted client-ID alphabet."""

    digest = hashlib.sha256(delivery_key.encode("utf-8")).hexdigest()
    return f"radar-{digest}"


def _sdk_object(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return dict(value)
