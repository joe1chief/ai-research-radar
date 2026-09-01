"""Dispatch breaking AI radar alerts to Telegram, Feishu/Lark, and Discord."""

from __future__ import annotations

import os
from typing import Any
import httpx

from .contracts import RadarEvent


def format_webhook_markdown(event: RadarEvent) -> str:
    topics_str = " · ".join(f"#{t}" for t in event.topics)
    return (
        f"🚨 **[AI Research Radar 突发突破] {event.title_zh}**\n\n"
        f"📊 **评分**: `{event.score}/100` | 🏷️ **主题**: {topics_str}\n"
        f"🔗 **一手来源**: {event.primary_url}\n\n"
        f"📝 **【深度研判】**\n{event.summary_zh}\n\n"
        f"💡 **【战略影响】**\n{event.why_it_matters}\n\n"
        f"🌐 *在线雷达: https://joe1chief.github.io/ai-research-radar/*"
    )


def dispatch_telegram_alert(event: RadarEvent, *, bot_token: str | None = None, chat_id: str | None = None) -> bool:
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    text = format_webhook_markdown(event)
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
            )
            return resp.is_success
    except Exception:
        return False


def dispatch_feishu_alert(event: RadarEvent, *, webhook_url: str | None = None) -> bool:
    url = webhook_url or os.getenv("FEISHU_WEBHOOK_URL")
    if not url:
        return False
    title = f"🚨 [AI 雷达预警 · {event.score}分] {event.title_zh}"
    content = [
        [{"tag": "text", "text": f"🏷️ 主题: {' · '.join(str(t) for t in event.topics)}\n"}],
        [{"tag": "text", "text": f"📝 深度研判:\n{event.summary_zh}\n\n"}],
        [{"tag": "text", "text": f"💡 为什么重要:\n{event.why_it_matters}\n\n"}],
        [{"tag": "a", "text": "🔗 查看一手信源", "href": event.primary_url}],
        [{"tag": "a", "text": " | 🌐 打开雷达大盘", "href": "https://joe1chief.github.io/ai-research-radar/"}],
    ]
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content,
                }
            }
        },
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            return resp.is_success
    except Exception:
        return False


def dispatch_discord_alert(event: RadarEvent, *, webhook_url: str | None = None) -> bool:
    url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    payload = {
        "embeds": [
            {
                "title": f"🚨 [{event.score}分] {event.title_zh}",
                "url": event.primary_url,
                "description": f"**深度研判**\n{event.summary_zh}\n\n**为什么重要**\n{event.why_it_matters}",
                "color": 5763719,
                "fields": [
                    {"name": "Topics", "value": " · ".join(str(t) for t in event.topics), "inline": True},
                    {"name": "Score", "value": f"{event.score}/100", "inline": True},
                ],
                "footer": {"text": "AI Research Radar"},
            }
        ]
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            return resp.is_success
    except Exception:
        return False


def dispatch_all_webhooks(event: RadarEvent) -> dict[str, bool]:
    return {
        "telegram": dispatch_telegram_alert(event),
        "feishu": dispatch_feishu_alert(event),
        "discord": dispatch_discord_alert(event),
    }
