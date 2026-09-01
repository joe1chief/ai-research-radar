"""Stable public data contracts shared by collectors, storage, email and web export."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Topic(StrEnum):
    LONG_HORIZON = "long_horizon"
    AUTONOMOUS_AGENT = "autonomous_agent"
    SELF_EVOLVING = "self_evolving"
    MECHANISTIC_INTERPRETABILITY = "mechanistic_interpretability"
    SAFETY_GOVERNANCE = "safety_governance"
    INDUSTRIAL_CAPITAL = "industrial_capital"
    PODCAST_CULTURE = "podcast_culture"


class EventStatus(StrEnum):
    NEW_ENTITY = "NEW_ENTITY"
    MATERIAL_UPDATE = "MATERIAL_UPDATE"
    MINOR_UPDATE = "MINOR_UPDATE"
    DISCOVERED_LATE = "DISCOVERED_LATE"


class VerificationStatus(StrEnum):
    VERIFIED_PRIMARY = "verified_primary"
    CORROBORATED = "corroborated"
    COMPANY_CLAIM = "company_claim"
    REPORTED_UNCONFIRMED = "reported_unconfirmed"


class DeliveryState(StrEnum):
    PENDING = "pending"
    DRAFT = "draft"
    COMPOSED = "composed"
    SHADOW = "shadow"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"
    FAILED = "failed"
    BOUNCED = "bounced"
    REJECTED = "rejected"
    COMPLAINED = "complained"


class SourceSpec(BaseModel):
    """Declarative source configuration from ``configs/sources.yml``."""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    entity_id: str
    group: str
    kind: str
    url: str
    fetch_strategy: str
    cadence: str = "daily"
    evidence_type: str
    cursor_strategy: str = "etag_last_modified"
    parser: str
    enabled: bool = True
    timeout_seconds: float = Field(30.0, gt=0, le=120)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("source URL must be HTTP(S)")
        return value


class CollectedItem(BaseModel):
    """Normalized output emitted by every collector."""

    source_id: str
    external_id: str
    canonical_url: str
    title: str
    summary: str = ""
    content: str = ""
    authors: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    updated_at: datetime | None = None
    entity_id: str | None = None
    evidence_type: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Ephemeral source bytes. They may be uploaded to private object storage,
    # but are deliberately excluded from model dumps and database JSON.
    raw_snapshot: bytes | None = Field(default=None, exclude=True, repr=False)

    @field_validator("published_at", "updated_at")
    @classmethod
    def ensure_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @field_validator("canonical_url")
    @classmethod
    def canonical_url_is_http(cls, value: str) -> str:
        if urlsplit(value).scheme.casefold() not in {"http", "https"}:
            raise ValueError("canonical URL must use HTTP(S)")
        return value


class RadarEvent(BaseModel):
    event_id: str
    cluster_id: str
    event_type: str
    topics: list[Topic] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    title_zh: str
    summary_zh: str
    why_it_matters: str = ""
    source_time: datetime | None = None
    first_seen_at: datetime
    material_updated_at: datetime | None = None
    status: EventStatus
    source_type: str
    evidence_type: str = "unknown"
    verification_status: VerificationStatus
    score: int = Field(ge=0, le=100)
    primary_url: str
    corroborating_urls: list[str] = Field(default_factory=list)
    cross_tags: list[str] = Field(default_factory=list)
    change_summary: str = ""
    arxiv_url: str | None = None
    alphaxiv_url: str | None = None
    code_url: str | None = None
    project_url: str | None = None

    @field_validator("source_time", "first_seen_at", "material_updated_at")
    @classmethod
    def event_datetimes_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @field_validator("primary_url", "arxiv_url", "alphaxiv_url", "code_url", "project_url")
    @classmethod
    def event_urls_are_http(cls, value: str | None) -> str | None:
        if value is not None and urlsplit(value).scheme.casefold() not in {"http", "https"}:
            raise ValueError("event URL must use HTTP(S)")
        return value

    @field_validator("corroborating_urls")
    @classmethod
    def corroborating_urls_are_http(cls, values: list[str]) -> list[str]:
        if any(urlsplit(value).scheme.casefold() not in {"http", "https"} for value in values):
            raise ValueError("corroborating URL must use HTTP(S)")
        return values


class Delivery(BaseModel):
    delivery_key: str
    event_revision_ids: list[str] = Field(default_factory=list)
    channel: str = "email"
    send_at: datetime | None = None
    state: DeliveryState = DeliveryState.PENDING
    agentmail_draft_id: str | None = None
    message_id: str | None = None
    delivered_at: datetime | None = None

    @field_validator("send_at", "delivered_at")
    @classmethod
    def delivery_datetimes_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class CollectionBatch(BaseModel):
    items: list[CollectedItem] = Field(default_factory=list)
    cursor: dict[str, Any] = Field(default_factory=dict)
    not_modified: bool = False
    warnings: list[str] = Field(default_factory=list)
