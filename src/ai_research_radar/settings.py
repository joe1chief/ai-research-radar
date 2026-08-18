"""Runtime settings loaded exclusively from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = Field("development", alias="APP_ENV")
    timezone: str = Field("Asia/Shanghai", alias="APP_TIMEZONE")
    database_url: str = Field("sqlite:///./radar.db", alias="RADAR_DATABASE_URL")
    config_dir: Path = Field(Path("configs"), alias="RADAR_CONFIG_DIR")
    public_data_path: Path = Field(
        Path("web/public/data/latest.json"), alias="RADAR_PUBLIC_DATA_PATH"
    )
    dry_run: bool = Field(True, alias="RADAR_DRY_RUN")
    user_agent: str = Field(
        "AIResearchRadar/0.1 contact=you@example.com", alias="RADAR_USER_AGENT"
    )

    dashscope_api_key: str | None = Field(None, alias="DASHSCOPE_API_KEY")
    dashscope_base_url: str = Field(
        "https://dashscope.aliyuncs.com/compatible-mode/v1", alias="DASHSCOPE_BASE_URL"
    )
    classifier_model: str = Field("qwen-flash", alias="QWEN_CLASSIFIER_MODEL")
    summarizer_model: str = Field("qwen-plus", alias="QWEN_SUMMARIZER_MODEL")
    embedding_model: str = Field("text-embedding-v4", alias="QWEN_EMBEDDING_MODEL")
    daily_classify_limit: int = Field(300, alias="QWEN_DAILY_CLASSIFY_LIMIT")
    daily_summary_limit: int = Field(20, alias="QWEN_DAILY_SUMMARY_LIMIT")
    daily_reembed_limit: int = Field(100, alias="QWEN_DAILY_REEMBED_LIMIT")

    alphaxiv_mcp_url: str = Field(
        "https://api.alphaxiv.org/mcp/v1", alias="ALPHAXIV_MCP_URL"
    )
    alphaxiv_access_token: str | None = Field(None, alias="ALPHAXIV_ACCESS_TOKEN")
    alphaxiv_daily_read_limit: int = Field(5, alias="ALPHAXIV_DAILY_READ_LIMIT")

    agentmail_api_key: str | None = Field(None, alias="AGENTMAIL_API_KEY")
    agentmail_inbox_id: str | None = Field(None, alias="AGENTMAIL_INBOX_ID")
    digest_recipient: str | None = Field(None, alias="DIGEST_RECIPIENT")
    delivery_mode: str = Field("shadow", alias="DELIVERY_MODE", pattern="^(shadow|live)$")

    github_token: str | None = Field(None, alias="GITHUB_TOKEN")
    openreview_access_token: str | None = Field(
        None, alias="OPENREVIEW_ACCESS_TOKEN"
    )

    supabase_url: str | None = Field(None, alias="SUPABASE_URL")
    supabase_secret_key: str | None = Field(None, alias="SUPABASE_SECRET_KEY")
    raw_storage_bucket: str = Field("radar-raw", alias="RADAR_RAW_STORAGE_BUCKET")
    raw_snapshot_max_bytes: int = Field(
        5 * 1024 * 1024, alias="RADAR_RAW_SNAPSHOT_MAX_BYTES"
    )


def get_settings(**overrides: object) -> Settings:
    """Create settings at call time so tests and CLI can change the environment."""

    return Settings(**overrides)
