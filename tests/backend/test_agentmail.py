from __future__ import annotations

import re
from datetime import timedelta

from ai_research_radar.agentmail import _draft_client_id, deliver_outbox, reconcile_drafts
from ai_research_radar.db import DeliveryModel, WebhookEventModel, utcnow


class FakeDraftClient:
    def __init__(self):
        self.created = []
        self.sent = []

    def create_draft(self, **kwargs):
        self.created.append(kwargs)
        return "draft-1"

    def send_draft(self, draft_id):
        self.sent.append(draft_id)
        return "message-1"

    def update_draft(self, draft_id, **kwargs):
        self.created.append({"updated_draft_id": draft_id, **kwargs})
        return draft_id

    def get_draft(self, draft_id):
        return {"draft_id": draft_id, "send_status": "scheduled"}

    def find_draft_by_label(self, label, *, after=None):
        return None

    def find_message_by_label(self, label, *, after=None):
        return None


def row(key="digest:x:2026-07-12", *, send_at=None):
    return DeliveryModel(
        delivery_key=key,
        recipient_hash="a" * 64,
        channel="agentmail",
        delivery_kind="digest",
        send_at=send_at,
        state="pending",
        metadata_json={"subject": "Radar", "text": "text", "html": "<p>text</p>"},
    )


def test_shadow_mode_never_calls_agentmail(session):
    delivery = row()
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()
    result = deliver_outbox(session, mode="shadow", client=client)
    assert result["shadow"] == 1
    assert delivery.state == "draft"
    assert delivery.metadata_json["shadow"] is True
    assert not client.created


def test_shadow_with_credentials_creates_review_draft_but_never_sends(session):
    delivery = row("digest:x:review")
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()
    result = deliver_outbox(
        session,
        mode="shadow",
        recipient="reviewer@example.com",
        client=client,
    )
    assert result["shadow"] == 1
    assert delivery.state == "draft"
    assert delivery.agentmail_draft_id == "draft-1"
    assert client.created[0]["client_id"] == _draft_client_id(delivery.delivery_key)
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", client.created[0]["client_id"])
    assert client.created[0]["send_at"] is None
    assert client.created[0]["labels"][0].startswith("radar-delivery-")
    assert not client.sent


def test_live_mode_schedules_future_draft_once(session):
    delivery = row(send_at=utcnow() + timedelta(hours=1))
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()
    result = deliver_outbox(
        session,
        mode="live",
        recipient="reader@example.com",
        client=client,
    )
    assert result["scheduled"] == 1
    assert delivery.state == "scheduled"
    assert delivery.agentmail_draft_id == "draft-1"
    assert client.created[0]["client_id"] == _draft_client_id(delivery.delivery_key)
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", client.created[0]["client_id"])
    assert client.created[0]["labels"][0].startswith("radar-delivery-")
    # A second run cannot recreate or send the draft.
    deliver_outbox(session, mode="live", recipient="reader@example.com", client=client)
    assert len(client.created) == 1


def test_draft_client_id_is_stable_unique_and_agentmail_safe():
    delivery_key = "digest:x:2026-07-12"
    client_id = _draft_client_id(delivery_key)

    assert client_id == (
        "radar-96613a86c991e38ef378504a9627350a"
        "6932b97196389c6738968df05c79c3af"
    )
    assert client_id == _draft_client_id(delivery_key)
    assert client_id != _draft_client_id("digest:x:2026-07-13")
    assert len(client_id) == 70
    assert client_id.startswith("radar-")
    assert ":" not in client_id
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", client_id)


def test_existing_draft_update_does_not_replace_client_id_or_label(session):
    delivery = row("digest:x:existing", send_at=utcnow() + timedelta(hours=1))
    delivery.agentmail_draft_id = "draft-existing"
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()

    result = deliver_outbox(
        session,
        mode="live",
        recipient="reader@example.com",
        client=client,
    )

    assert result["scheduled"] == 1
    assert client.created == [
        {
            "updated_draft_id": "draft-existing",
            "to": ["reader@example.com"],
            "subject": "Radar",
            "text": "text",
            "html": "<p>text</p>",
            "send_at": delivery.send_at,
        }
    ]
    assert "client_id" not in client.created[0]
    assert delivery.metadata_json["agentmail_label"].startswith("radar-delivery-")


def test_live_mode_sends_due_alert(session):
    delivery = row("alert:x:revision", send_at=None)
    delivery.delivery_kind = "alert"
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()
    result = deliver_outbox(
        session,
        mode="live",
        recipient="reader@example.com",
        client=client,
    )
    assert result["sent"] == 1
    assert delivery.agentmail_message_id == "message-1"
    assert client.sent == ["draft-1"]


def test_retryable_draft_creation_failure_stays_pending_for_safe_replay(session):
    delivery = row("digest:x:retryable", send_at=utcnow() + timedelta(hours=1))
    session.add(delivery)
    session.flush()

    class RetryableError(RuntimeError):
        status_code = 503

    class FailingCreateClient(FakeDraftClient):
        def create_draft(self, **kwargs):
            raise RetryableError("temporary provider failure")

    result = deliver_outbox(
        session,
        mode="live",
        recipient="reader@example.com",
        client=FailingCreateClient(),
    )
    assert result["failed"] == 1
    assert delivery.state == "pending"
    assert delivery.agentmail_draft_id is None


def test_late_digest_handles_sqlite_naive_utc_and_marks_subject(session):
    delivery = row("digest:x:late", send_at=(utcnow() - timedelta(minutes=5)).replace(tzinfo=None))
    session.add(delivery)
    session.flush()
    client = FakeDraftClient()
    result = deliver_outbox(
        session,
        mode="live",
        recipient="reader@example.com",
        client=client,
    )
    assert result["sent"] == 1
    assert client.created[0]["subject"].startswith("[延迟日报]")


def test_reconcile_scheduled_draft_by_label_and_apply_early_delivery_webhook(session):
    delivery = row("digest:x:scheduled", send_at=utcnow() - timedelta(minutes=20))
    delivery.state = "scheduled"
    delivery.agentmail_draft_id = "draft-gone"
    delivery.metadata_json = {
        **delivery.metadata_json,
        "agentmail_label": "radar-delivery:known",
    }
    session.add(delivery)
    session.add(
        WebhookEventModel(
            provider_event_id="evt-delivered",
            event_type="message.delivered",
            message_id="message-scheduled",
            signature_verified=True,
            payload={"event_type": "message.delivered"},
        )
    )
    session.flush()

    class SentClient(FakeDraftClient):
        def get_draft(self, draft_id):
            raise LookupError("scheduled draft was consumed")

        def find_message_by_label(self, label, *, after=None):
            assert label == "radar-delivery:known"
            return {"message_id": "message-scheduled", "labels": [label]}

    result = reconcile_drafts(session, SentClient())
    assert result["delivered"] == 1
    assert delivery.agentmail_message_id == "message-scheduled"
    assert delivery.state == "delivered"
    webhook = session.get(WebhookEventModel, "evt-delivered")
    assert webhook.delivery_key == delivery.delivery_key


def test_reconcile_retries_same_immediate_draft_when_it_still_exists(session):
    delivery = row("alert:x:unknown")
    delivery.delivery_kind = "alert"
    delivery.state = "unknown"
    delivery.agentmail_draft_id = "draft-still-exists"
    session.add(delivery)
    session.flush()

    class ExistingDraftClient(FakeDraftClient):
        def get_draft(self, draft_id):
            return {"draft_id": draft_id, "send_status": None}

    client = ExistingDraftClient()
    result = reconcile_drafts(session, client)
    assert result["sent"] == 1
    assert delivery.state == "sent"
    assert delivery.agentmail_message_id == "message-1"
    assert client.sent == ["draft-still-exists"]


def test_reconcile_keeps_retryable_send_provider_failure_unknown(session):
    delivery = row("alert:x:retryable-unknown")
    delivery.delivery_kind = "alert"
    delivery.state = "unknown"
    delivery.agentmail_draft_id = "draft-still-exists"
    session.add(delivery)
    session.flush()

    class RetryableError(RuntimeError):
        status_code = 503

    class FailingSendClient(FakeDraftClient):
        def get_draft(self, draft_id):
            return {"draft_id": draft_id, "send_status": None}

        def send_draft(self, draft_id):
            raise RetryableError("provider unavailable after send attempt")

    result = reconcile_drafts(session, FailingSendClient())
    assert result["unknown"] == 1
    assert result["failed"] == 0
    assert delivery.state == "unknown"


def test_reconcile_never_sends_a_shadow_review_draft(session):
    delivery = row("digest:x:shadow-timeout")
    delivery.state = "unknown"
    delivery.agentmail_draft_id = "shadow-draft"
    delivery.metadata_json = {**delivery.metadata_json, "shadow": True}
    session.add(delivery)
    session.flush()

    class ShadowDraftClient(FakeDraftClient):
        def get_draft(self, draft_id):
            return {"draft_id": draft_id, "send_status": None}

    client = ShadowDraftClient()
    result = reconcile_drafts(session, client)
    assert result["draft"] == 1
    assert delivery.state == "draft"
    assert client.sent == []
