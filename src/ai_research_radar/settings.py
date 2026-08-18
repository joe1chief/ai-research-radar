"""Runtime settings loaded exclusively from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
YICLOUD_BASE_URL = "https://token-api.yicloud.com/v1"


def _normalize_optional_trailing_slash(value: str) -> str:
    """Normalize one harmless trailing slash without accepting a different path."""

    if value.endswith("/") and not value.endswith("//"):
        return value[:-1]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

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
    sec_user_agent: str | None = Field(None, alias="SEC_USER_AGENT")

    llm_provider: Literal["dashscope", "yicloud"] = Field(
        "dashscope", alias="LLM_PROVIDER"
    )
    llm_api_key: SecretStr | None = Field(None, alias="LLM_API_KEY")
    legacy_dashscope_api_key: SecretStr | None = Field(
        None,
        alias="DASHSCOPE_API_KEY",
        exclude=True,
        repr=False,
    )
    llm_base_url: str = Field("", alias="LLM_BASE_URL")
    legacy_dashscope_base_url: str | None = Field(
        None,
        alias="DASHSCOPE_BASE_URL",
        exclude=True,
        repr=False,
    )
    classifier_model: str = Field(
        "qwen-flash",
        validation_alias=AliasChoices("LLM_CLASSIFIER_MODEL", "QWEN_CLASSIFIER_MODEL"),
    )
    summarizer_model: str = Field(
        "qwen-plus",
        validation_alias=AliasChoices("LLM_SUMMARIZER_MODEL", "QWEN_SUMMARIZER_MODEL"),
    )
    llm_enable_thinking: bool | None = Field(None, alias="LLM_ENABLE_THINKING")
    llm_json_response_format: bool = Field(True, alias="LLM_JSON_RESPONSE_FORMAT")
    llm_max_tokens: int = Field(1200, alias="LLM_MAX_TOKENS", ge=64, le=4096)

    embedding_mode: Literal["shared", "remote", "local"] = Field(
        "shared", alias="LLM_EMBEDDING_MODE"
    )
    embedding_api_key: SecretStr | None = Field(None, alias="LLM_EMBEDDING_API_KEY")
    embedding_base_url: str | None = Field(None, alias="LLM_EMBEDDING_BASE_URL")
    embedding_model: str = Field(
        "text-embedding-v4",
        validation_alias=AliasChoices("LLM_EMBEDDING_MODEL", "QWEN_EMBEDDING_MODEL"),
    )
    embedding_dimensions: int = Field(
        1024,
        validation_alias=AliasChoices(
            "LLM_EMBEDDING_DIMENSIONS", "QWEN_EMBEDDING_DIMENSIONS"
        ),
        ge=1024,
        le=1024,
    )
    daily_classify_limit: int = Field(
        300,
        validation_alias=AliasChoices(
            "LLM_DAILY_CLASSIFY_LIMIT", "QWEN_DAILY_CLASSIFY_LIMIT"
        ),
    )
    daily_summary_limit: int = Field(
        20,
        validation_alias=AliasChoices(
            "LLM_DAILY_SUMMARY_LIMIT", "QWEN_DAILY_SUMMARY_LIMIT"
        ),
    )
    daily_reembed_limit: int = Field(
        100,
        validation_alias=AliasChoices(
            "LLM_DAILY_REEMBED_LIMIT", "QWEN_DAILY_REEMBED_LIMIT"
        ),
    )

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

    @model_validator(mode="after")
    def validate_model_endpoints(self) -> Settings:
        """Keep provider credentials pinned to their declared service."""

        new_key = self.llm_api_key.get_secret_value() if self.llm_api_key else None
        legacy_key = (
            self.legacy_dashscope_api_key.get_secret_value()
            if self.legacy_dashscope_api_key
            else None
        )
        if new_key and legacy_key and new_key != legacy_key:
            raise ValueError("LLM_API_KEY conflicts with DASHSCOPE_API_KEY")

        new_base = _normalize_optional_trailing_slash(self.llm_base_url)
        legacy_base = (
            _normalize_optional_trailing_slash(self.legacy_dashscope_base_url)
            if self.legacy_dashscope_base_url
            else None
        )
        if new_base and legacy_base and new_base != legacy_base:
            raise ValueError("LLM_BASE_URL conflicts with DASHSCOPE_BASE_URL")

        expected = {
            "dashscope": DASHSCOPE_BASE_URL,
            "yicloud": YICLOUD_BASE_URL,
        }[self.llm_provider]
        if self.llm_provider == "yicloud":
            if legacy_key or legacy_base:
                raise ValueError(
                    "YiCloud requires LLM_API_KEY and LLM_BASE_URL; "
                    "DASHSCOPE_* aliases are not accepted"
                )
            if not new_base:
                raise ValueError("LLM_BASE_URL is required when LLM_PROVIDER=yicloud")
            missing_model_roles = [
                env_name
                for field_name, env_name, value in (
                    (
                        "classifier_model",
                        "LLM_CLASSIFIER_MODEL",
                        self.classifier_model,
                    ),
                    (
                        "summarizer_model",
                        "LLM_SUMMARIZER_MODEL",
                        self.summarizer_model,
                    ),
                )
                if field_name not in self.model_fields_set
                or not value.strip()
                or value.startswith("required-yicloud-")
            ]
            if missing_model_roles:
                raise ValueError(
                    "YiCloud requires account-verified model IDs via "
                    + " and ".join(missing_model_roles)
                )
        else:
            if not new_key and self.legacy_dashscope_api_key:
                self.llm_api_key = self.legacy_dashscope_api_key
            if not new_base:
                new_base = legacy_base or DASHSCOPE_BASE_URL
                self.llm_base_url = new_base

        if new_base != expected:
            raise ValueError(
                f"LLM_BASE_URL must be {expected} when LLM_PROVIDER={self.llm_provider}"
            )
        if self.embedding_mode == "remote":
            if not self.embedding_api_key:
                raise ValueError(
                    "LLM_EMBEDDING_API_KEY is required when LLM_EMBEDDING_MODE=remote"
                )
            if not self.embedding_base_url:
                raise ValueError(
                    "LLM_EMBEDDING_BASE_URL is required when LLM_EMBEDDING_MODE=remote"
                )
            parsed = urlsplit(self.embedding_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "LLM_EMBEDDING_BASE_URL must be an https URL without "
                    "userinfo, query, or fragment"
                )
        return self

    @property
    def effective_enable_thinking(self) -> bool | None:
        """DashScope retains its explicit opt-out; YiCloud omits the extension."""

        if self.llm_enable_thinking is not None:
            return self.llm_enable_thinking
        return False if self.llm_provider == "dashscope" else None

    @property
    def llm_api_key_value(self) -> str | None:
        return self.llm_api_key.get_secret_value() if self.llm_api_key else None

    @property
    def embedding_api_key_value(self) -> str | None:
        return (
            self.embedding_api_key.get_secret_value()
            if self.embedding_api_key
            else None
        )

    # Attribute compatibility for integrations that imported the old settings names.
    @property
    def dashscope_api_key(self) -> str | None:
        return self.llm_api_key_value

    @property
    def dashscope_base_url(self) -> str:
        return self.llm_base_url


def get_settings(**overrides: object) -> Settings:
    """Create settings at call time so tests and CLI can change the environment."""

    return Settings(**overrides)
