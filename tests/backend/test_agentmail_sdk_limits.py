"""Exercise the locked SDK through MockTransport, rather than mocking its API."""

import httpx
import pytest
from agentmail import AgentMail

from ai_research_radar.agentmail import AgentMailClient, deliver_outbox
from ai_research_radar.db import DeliveryModel


def adapter(reply):
    value = AgentMailClient(api_key="unused-test-key", inbox_id="test-inbox")
    value.client = AgentMail(
        api_key="unused-test-key", httpx_client=httpx.Client(transport=httpx.MockTransport(reply))
    )
    return value


@pytest.mark.parametrize("status", [500, 408, 409])
def test_non_idempotent_sdk_send_is_one_post_and_unknown(session, status):
    calls = []

    def reply(request):
        calls.append(request)
        if request.url.path.endswith("/send"):
            return httpx.Response(
                status, json={"message": "synthetic provider error"}, request=request
            )
        return httpx.Response(200, json={"draft_id": "test-draft"}, request=request)

    client = adapter(reply)
    delivery = DeliveryModel(
        delivery_key="alert:test",
        recipient_hash="test",
        channel="agentmail",
        delivery_kind="alert",
        state="pending",
        metadata_json={"text": "test"},
    )
    session.add(delivery)
    session.commit()
    result = deliver_outbox(session, mode="live", recipient="test@example.invalid", client=client)
    assert result["unknown"] == 1 and delivery.state == "unknown"
    assert delivery.agentmail_draft_id == "test-draft"
    assert len([r for r in calls if r.url.path.endswith("/send")]) == 1
    assert all(r.extensions["timeout"]["read"] <= 20 for r in calls)
    session.commit()
    deliver_outbox(session, mode="live", recipient="test@example.invalid", client=client)
    assert len([r for r in calls if r.url.path.endswith("/send")]) == 1


def test_safe_get_retries_are_only_adapter_attempts(monkeypatch):
    calls, sleeps = [], []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            500 if len(calls) < 3 else 200, json={"draft_id": "test"}, request=request
        )

    monkeypatch.setattr("ai_research_radar.agentmail.time.sleep", sleeps.append)
    client = adapter(reply)
    assert client.get_draft("test")["draft_id"] == "test"
    assert len(calls) == 3 and len(sleeps) == 2


def test_rate_limit_can_retry_explicit_rejection_without_sdk_retries(monkeypatch):
    calls, sleeps = [], []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            429 if len(calls) == 1 else 200,
            headers={"Retry-After": "1"},
            json={"message_id": "message-test"},
            request=request,
        )

    monkeypatch.setattr("ai_research_radar.agentmail.time.sleep", sleeps.append)
    assert adapter(reply).send_draft("test") == "message-test"
    assert len(calls) == 2 and len(sleeps) == 1 and sleeps[0] >= 1


def test_sdk_long_retry_after_does_not_sleep_or_make_early_request(monkeypatch):
    calls, sleeps = [], []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "7200"}, json={"message": "limited"}, request=request
        )

    monkeypatch.setattr("ai_research_radar.agentmail.time.sleep", sleeps.append)
    with pytest.raises(Exception):
        adapter(reply).get_draft("test")
    assert len(calls) == 1 and sleeps == []
