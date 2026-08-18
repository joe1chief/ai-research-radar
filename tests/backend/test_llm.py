from __future__ import annotations

import json
import math

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_research_radar import cli
from ai_research_radar.contracts import CollectedItem, Topic
from ai_research_radar.llm import (
    ModelClient,
    ModelSmokeError,
    QwenClient,
    deterministic_embedding,
)
from ai_research_radar.settings import DASHSCOPE_BASE_URL, YICLOUD_BASE_URL, Settings


def test_no_key_is_deterministic_offline_fallback():
    client = QwenClient(api_key=None, base_url="https://dashscope.example/v1")
    assert client.client.follow_redirects is False
    item = CollectedItem(
        source_id="x", external_id="x", canonical_url="https://example.com/x", title="Agent"
    )
    assert client.enhance(item, [Topic.AUTONOMOUS_AGENT]) is None
    assert client.adjudicate_merge("a", "b") is None
    assert client.embed("agent memory") == deterministic_embedding("agent memory")
    assert len(client.embed("agent memory")) == 1024
    client.close()


def test_qwen_json_is_allowlisted_and_merge_adjudication_is_validated():
    requested_models = []
    responses = iter(
        [
            {
                "choices": [{"message": {"content": json.dumps({
                    "topics": ["autonomous_agent", "industrial_capital"],
                    "title_zh": "智能体发布",
                    "summary_zh": "官方发布了智能体运行时。",
                    "why_it_matters": "更新了自治智能体基础设施。",
                })}}]
            },
            {
                "choices": [{"message": {"content": json.dumps({
                    "title_zh": "智能体运行时正式发布",
                    "summary_zh": "官方发布了可复核的多智能体运行时。",
                    "why_it_matters": "它更新了自治智能体基础设施。",
                })}}]
            },
            {"choices": [{"message": {"content": '{"same_event": true, "reason": "same release"}'}}]},
        ]
    )

    def handler(request):
        requested_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=next(responses), request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    qwen = QwenClient(api_key="secret", base_url="https://dashscope.example/v1", client=http)
    item = CollectedItem(
        source_id="x",
        external_id="x",
        canonical_url="https://example.com/x",
        title="Agent runtime",
        summary="multi-agent release",
    )
    result = qwen.enhance(item, [Topic.AUTONOMOUS_AGENT])
    assert result.topics == [Topic.AUTONOMOUS_AGENT]
    assert result.title_zh == "智能体运行时正式发布"
    adjudication = qwen.adjudicate_merge("release A", "release A copy")
    assert adjudication and adjudication.same_event
    assert requested_models == ["qwen-flash", "qwen-plus", "qwen-flash"]


def test_qwen_failure_keeps_gray_zone_separate():
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    )
    qwen = QwenClient(api_key="secret", base_url="https://dashscope.example/v1", client=http)
    assert qwen.adjudicate_merge("same topic", "same topic") is None
    embedded = qwen.embed_with_provenance("agent memory")
    assert embedded.space == "feature-hash-v1"
    assert embedded.vector == deterministic_embedding("agent memory")


def test_provider_neutral_settings_keep_dashscope_aliases_and_reject_conflicts():
    legacy = Settings(
        _env_file=None,
        DASHSCOPE_API_KEY="legacy-secret",
        DASHSCOPE_BASE_URL=DASHSCOPE_BASE_URL,
        QWEN_CLASSIFIER_MODEL="legacy-classifier",
        QWEN_SUMMARIZER_MODEL="legacy-summarizer",
        QWEN_EMBEDDING_MODEL="legacy-embedding",
    )
    assert legacy.llm_api_key_value == "legacy-secret"
    assert legacy.llm_base_url == DASHSCOPE_BASE_URL
    assert legacy.classifier_model == "legacy-classifier"
    assert legacy.summarizer_model == "legacy-summarizer"
    assert legacy.embedding_model == "legacy-embedding"
    assert "legacy-secret" not in repr(legacy)

    with pytest.raises(ValidationError, match="conflicts with DASHSCOPE_API_KEY"):
        Settings(
            _env_file=None,
            LLM_API_KEY="new-secret",
            DASHSCOPE_API_KEY="different-secret",
        )


def test_yicloud_requires_normalized_key_and_exact_pinned_url():
    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="yicloud",
        LLM_API_KEY="normalized-secret",
        LLM_BASE_URL=f"{YICLOUD_BASE_URL}/",
        LLM_CLASSIFIER_MODEL="verified-classifier",
        LLM_SUMMARIZER_MODEL="verified-summarizer",
        LLM_EMBEDDING_MODE="local",
    )
    assert settings.llm_api_key_value == "normalized-secret"
    assert settings.effective_enable_thinking is None
    keyless_non_model_step = Settings(
        _env_file=None,
        LLM_PROVIDER="yicloud",
        LLM_BASE_URL=YICLOUD_BASE_URL,
        LLM_CLASSIFIER_MODEL="verified-classifier",
        LLM_SUMMARIZER_MODEL="verified-summarizer",
        LLM_EMBEDDING_MODE="local",
    )
    assert keyless_non_model_step.llm_api_key_value is None

    with pytest.raises(ValidationError, match=r"DASHSCOPE_\* aliases are not accepted"):
        Settings(
            _env_file=None,
            LLM_PROVIDER="yicloud",
            DASHSCOPE_API_KEY="wrong-alias",
            LLM_BASE_URL=YICLOUD_BASE_URL,
            LLM_CLASSIFIER_MODEL="verified-classifier",
            LLM_SUMMARIZER_MODEL="verified-summarizer",
        )
    for unsafe_url in (
        DASHSCOPE_BASE_URL,
        "http://token-api.yicloud.com/v1",
        "https://token-api.yicloud.com/v1?token=bad",
        "https://user@token-api.yicloud.com/v1",
        "https://token-api.yicloud.com/wrong",
        "https://token-api.yicloud.com/v1//",
    ):
        with pytest.raises(ValidationError, match="LLM_BASE_URL must be"):
            Settings(
                _env_file=None,
                LLM_PROVIDER="yicloud",
                LLM_API_KEY="normalized-secret",
                LLM_BASE_URL=unsafe_url,
                LLM_CLASSIFIER_MODEL="verified-classifier",
                LLM_SUMMARIZER_MODEL="verified-summarizer",
            )


def test_yicloud_never_inherits_unconfigured_dashscope_model_defaults():
    with pytest.raises(ValidationError, match="account-verified model IDs"):
        Settings(
            _env_file=None,
            LLM_PROVIDER="yicloud",
            LLM_API_KEY="normalized-secret",
            LLM_BASE_URL=YICLOUD_BASE_URL,
            LLM_EMBEDDING_MODE="local",
        )

    with pytest.raises(ValidationError, match="LLM_SUMMARIZER_MODEL"):
        Settings(
            _env_file=None,
            LLM_PROVIDER="yicloud",
            LLM_API_KEY="normalized-secret",
            LLM_BASE_URL=YICLOUD_BASE_URL,
            LLM_CLASSIFIER_MODEL="verified-classifier",
            LLM_SUMMARIZER_MODEL="required-yicloud-summarizer-model",
            LLM_EMBEDDING_MODE="local",
        )


def test_yicloud_payload_omits_vendor_thinking_extension():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "enable_thinking" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 1200
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "topics": [],
                                    "title_zh": "",
                                    "summary_zh": "",
                                    "why_it_matters": "",
                                }
                            )
                        }
                    }
                ]
            },
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ModelClient(
        api_key="secret",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="local",
        enable_thinking=None,
        client=http,
    )
    item = CollectedItem(
        source_id="x",
        external_id="x",
        canonical_url="https://example.com/x",
        title="Agent runtime",
    )
    assert client.enhance(item, [Topic.AUTONOMOUS_AGENT], summarize=False) is not None


def test_non_string_chat_content_preserves_operational_fail_soft_behavior():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}}]},
            request=request,
        )

    client = ModelClient(
        api_key="secret",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="local",
        enable_thinking=None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    item = CollectedItem(
        source_id="x",
        external_id="x",
        canonical_url="https://example.com/x",
        title="Agent runtime",
    )
    assert client.enhance(item, [Topic.AUTONOMOUS_AGENT], summarize=False) is None


def test_embedding_modes_are_decoupled_and_local_never_calls_remote():
    local_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"unexpected request: {request.url}")
        )
    )
    local = ModelClient(
        api_key="chat-key",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="local",
        enable_thinking=None,
        client=local_http,
    )
    local_result = local.embed_with_provenance("agent memory")
    assert local_result.space == "feature-hash-v1"
    assert local.remote_embedding_enabled is False

    def remote_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://embedding.example/v1/embeddings")
        assert request.headers["Authorization"] == "Bearer embedding-key"
        return httpx.Response(
            200,
            json={"data": [{"embedding": [1.0, *([0.0] * 1023)]}]},
            request=request,
        )

    remote_http = httpx.Client(transport=httpx.MockTransport(remote_handler))
    remote = ModelClient(
        api_key="chat-key",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="remote",
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="embed-v1",
        client=remote_http,
    )
    remote_result = remote.embed_with_provenance("agent memory")
    assert remote_result.space == "embedding.example:embed-v1:1024"
    assert remote.remote_embedding_enabled is True


@pytest.mark.parametrize(
    "vector",
    [
        [0.0] * 1024,
        [math.nan, *([0.0] * 1023)],
        [math.inf, *([0.0] * 1023)],
    ],
)
def test_invalid_remote_embedding_falls_back_and_remains_retryable(vector):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"embedding": vector}]},
            request=request,
        )

    client = ModelClient(
        api_key="chat-key",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="remote",
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="embed-v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.embed_with_provenance("agent memory")
    assert result.space == "feature-hash-v1"
    assert result.vector == deterministic_embedding("agent memory")
    assert client.remote_embedding_enabled is True


def test_strict_smoke_validates_models_structured_chat_and_remote_embedding():
    requested_chat_models = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "classifier"}, {"id": "summarizer"}]},
                request=request,
            )
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            requested_chat_models.append(payload["model"])
            assert payload["max_tokens"] == 1200
            assert payload["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"ok":true,"message":"radar-model-smoke"}'
                            }
                        }
                    ]
                },
                request=request,
            )
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            json={"data": [{"embedding": [1.0, *([0.0] * 1023)]}]},
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ModelClient(
        api_key="secret",
        base_url="https://models.example/v1",
        classifier_model="classifier",
        summarizer_model="summarizer",
        embedding_model="embedding",
        client=http,
    )
    result = client.smoke_test()
    assert result.models_visible == 2
    assert result.models_status == "ok"
    assert requested_chat_models == ["classifier", "summarizer"]
    assert result.embedding_checked is True
    assert result.embedding_dimensions == 1024


def test_strict_smoke_rejects_zero_norm_remote_embedding():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"ok":true,"message":"radar-model-smoke"}'
                            }
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "classifier"}, {"id": "summarizer"}]},
                request=request,
            )
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.0] * 1024}]},
            request=request,
        )

    client = ModelClient(
        api_key="secret",
        base_url="https://models.example/v1",
        classifier_model="classifier",
        summarizer_model="summarizer",
        embedding_model="embedding",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(
        ModelSmokeError,
        match="embedding_norm_not_positive_finite",
    ):
        client.smoke_test()


def test_strict_smoke_rejects_redirects_and_redacts_key_and_response_body(
    monkeypatch,
):
    secret = "never-print-this-key"
    response_body = "never-print-this-response"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=response_body,
            headers={"Location": "https://attacker.example/models"},
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    client = ModelClient(
        api_key=secret,
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="local",
        enable_thinking=None,
        client=http,
    )
    with pytest.raises(ModelSmokeError) as exc_info:
        client.smoke_test()
    assert exc_info.value.code == "http_status_401"
    assert secret not in str(exc_info.value)
    assert response_body not in str(exc_info.value)

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="yicloud",
        LLM_API_KEY=secret,
        LLM_BASE_URL=YICLOUD_BASE_URL,
        LLM_CLASSIFIER_MODEL="classifier",
        LLM_SUMMARIZER_MODEL="summarizer",
        LLM_EMBEDDING_MODE="local",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_model_client", lambda _settings: client)
    result = CliRunner().invoke(cli.app, ["model-smoke"])
    assert result.exit_code == 1
    assert '"stage": "classifier_chat"' in result.stdout
    assert '"error": "http_status_401"' in result.stdout
    assert secret not in result.stdout
    assert response_body not in result.stdout


def test_strict_smoke_rejects_redirect_status_without_following_it():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example/models"},
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    client = ModelClient(
        api_key="secret",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        embedding_mode="local",
        client=http,
    )
    with pytest.raises(ModelSmokeError, match="redirect_rejected"):
        client.smoke_test()
    assert calls == [f"{YICLOUD_BASE_URL}/chat/completions"]


def test_model_smoke_redacts_invalid_settings(monkeypatch):
    leaked_url_value = "must-not-appear"
    monkeypatch.setenv("LLM_PROVIDER", "yicloud")
    monkeypatch.setenv("LLM_API_KEY", "must-not-print-key")
    monkeypatch.setenv(
        "LLM_BASE_URL",
        f"{YICLOUD_BASE_URL}?token={leaked_url_value}",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    result = CliRunner().invoke(cli.app, ["model-smoke"])
    assert result.exit_code == 2
    assert '"error": "invalid_model_configuration"' in result.stdout
    assert "must-not-print-key" not in result.stdout
    assert leaked_url_value not in result.stdout


def test_models_endpoint_is_diagnostic_after_both_strict_chat_probes(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"ok":true,"message":"radar-model-smoke"}'
                            }
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(404, text="not supported", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ModelClient(
        api_key="secret",
        base_url=YICLOUD_BASE_URL,
        provider="yicloud",
        classifier_model="classifier",
        summarizer_model="summarizer",
        embedding_mode="local",
        enable_thinking=None,
        client=http,
    )
    result = client.smoke_test()
    assert result.models_status == "unavailable_http_status_404"
    assert result.models_visible is None
    assert calls == [
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
    ]
    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="yicloud",
        LLM_API_KEY="secret",
        LLM_BASE_URL=YICLOUD_BASE_URL,
        LLM_CLASSIFIER_MODEL="classifier",
        LLM_SUMMARIZER_MODEL="summarizer",
        LLM_EMBEDDING_MODE="local",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_model_client", lambda _settings: client)
    cli_result = CliRunner().invoke(cli.app, ["model-smoke"])
    assert cli_result.exit_code == 0
    assert '"classifier_chat": "ok"' in cli_result.stdout
    assert '"summarizer_chat": "ok"' in cli_result.stdout
    assert '"embedding": "local_intentional"' in cli_result.stdout
