from __future__ import annotations

import json

import httpx

from ai_research_radar.contracts import CollectedItem, Topic
from ai_research_radar.llm import QwenClient, deterministic_embedding


def test_no_key_is_deterministic_offline_fallback():
    client = QwenClient(api_key=None, base_url="https://dashscope.example/v1")
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
