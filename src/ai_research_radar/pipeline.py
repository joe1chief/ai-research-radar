"""Incremental collection and enrichment orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .collectors import UnsupportedCollectorError, collector_for
from .collectors.base import CollectorHTTPError, DomainRequestThrottle
from .config import load_issuers
from .contracts import EventStatus, RadarEvent, SourceSpec, Topic, VerificationStatus
from .db import (
    EventItemModel,
    EventRevisionModel,
    ItemModel,
    ItemVersionModel,
    RadarEventModel,
    SourceModel,
    current_item_version,
    ensure_cursor,
    ensure_source_health,
    ingest_item,
    reserve_daily_usage,
    sync_source,
    utcnow,
)
from .dedupe import ClusterDecision, change_summary, cluster_decision, cosine_similarity
from .identity import canonicalize_url, content_hash, normalize_content, stable_id
from .llm import QwenClient, deterministic_embedding
from .raw_storage import RawSnapshotStore
from .scoring import score_event
from .topics import RuleTopicClassifier, infer_event_type


LOGGER = logging.getLogger(__name__)


CAPITAL_EVENT_TYPES = {
    "IPO_FILING",
    "RAISE",
    "M_AND_A",
    "CAPEX_COMPUTE",
    "MATERIAL_CONTRACT",
    "EARNINGS_GUIDANCE",
    "OWNERSHIP",
    "REGULATORY_EXPORT",
}

# The SEC publishes a 10 requests/second ceiling. A shared 250 ms reservation
# interval keeps this process at a conservative maximum of four requests/second
# across data.sec.gov and www.sec.gov, including retries and redirects.
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.25


@dataclass(slots=True)
class CollectionStats:
    sources: int = 0
    discovered: int = 0
    changed: int = 0
    unchanged: int = 0
    not_modified: int = 0
    skipped: int = 0
    failed: int = 0
    degraded: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def collect_group(
    session: Session,
    sources: list[SourceSpec],
    *,
    group: str,
    user_agent: str,
    sec_user_agent: str | None = None,
    shared_client: httpx.Client | None = None,
    force: bool = False,
    github_token: str | None = None,
    openreview_access_token: str | None = None,
    raw_store: RawSnapshotStore | None = None,
    cursor_transform: Callable[[SourceSpec, dict[str, Any]], dict[str, Any]] | None = None,
    archive_only_cutoff: datetime | None = None,
) -> CollectionStats:
    """Collect one source group while keeping network waits outside DB transactions.

    Each source is committed independently. This preserves healthy results when
    another source fails and prevents a Supabase/PostgreSQL connection from
    sitting inside a transaction during potentially long HTTP collection.
    """

    stats = CollectionStats()
    sec_throttle = DomainRequestThrottle(SEC_MIN_REQUEST_INTERVAL_SECONDS)
    for spec in sources:
        if spec.group != group:
            continue
        source_row = sync_source(session, spec)
        if not spec.enabled:
            # Disabled sources still need their version-controlled state
            # synchronized. Otherwise a source disabled after production
            # failures remains enabled/failing forever in the database and in
            # maintenance reporting.
            health = ensure_source_health(session, spec.id)
            health.status = "disabled"
            health.consecutive_failures = 0
            health.last_http_status = None
            health.last_error = None
            health.updated_at = utcnow()
            source_row.next_due_at = None
            session.commit()
            continue
        stats.sources += 1
        now = utcnow()
        due = source_row.next_due_at
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if not force and due is not None and due > now:
            stats.skipped += 1
            session.commit()
            continue
        cursor_row = ensure_cursor(session, spec.id)
        health = ensure_source_health(session, spec.id)
        health.last_attempt_at = utcnow()
        cursor_payload = _cursor_payload(cursor_row)
        # Release the checked-out connection before the collector performs any
        # external I/O. The success/failure result is persisted in a fresh,
        # short transaction below.
        session.commit()
        if cursor_transform is not None:
            cursor_payload = cursor_transform(spec, dict(cursor_payload))
        try:
            collector_user_agent = user_agent
            if spec.kind == "sec_submissions":
                collector_user_agent = sec_user_agent or user_agent
            collector_kwargs: dict[str, Any] = {
                "client": shared_client,
                "user_agent": collector_user_agent,
                "authorization": _source_authorization(
                    spec.kind,
                    github_token=github_token,
                    openreview_access_token=openreview_access_token,
                ),
            }
            if spec.kind == "sec_submissions":
                collector_kwargs["request_throttle"] = sec_throttle.wait
            collector = collector_for(spec, **collector_kwargs)
        except UnsupportedCollectorError as exc:
            stats.skipped += 1
            health = ensure_source_health(session, spec.id)
            health.status = "degraded"
            health.last_error = str(exc)
            health.updated_at = utcnow()
            session.commit()
            continue
        try:
            # A single HTML/API source can spend minutes doing retries and
            # detail requests, so collection deliberately runs without an
            # active database transaction.
            batch = collector.collect(cursor_payload)
            source_discovered = 0
            source_changed = 0
            source_unchanged = 0
            # A savepoint prevents one malformed item from poisoning the
            # source transaction before its failure health is recorded.
            with session.begin_nested():
                source_row = session.get(SourceModel, spec.id)
                if source_row is None:
                    raise RuntimeError(f"source disappeared during collection: {spec.id}")
                cursor_row = ensure_cursor(session, spec.id)
                health = ensure_source_health(session, spec.id)
                for item in batch.items:
                    row, changed = ingest_item(session, spec, item)
                    source_discovered += 1
                    item_metadata = dict(row.metadata_json or {})
                    if archive_only_cutoff is not None and (
                        item_metadata.get("processed_hash") != row.current_content_hash
                    ):
                        item_metadata["pending_archive_only"] = {
                            "content_hash": row.current_content_hash,
                            "source_time_cutoff": archive_only_cutoff.isoformat(),
                        }
                    elif changed:
                        # A genuinely newer normal collection must not inherit
                        # an unfinished backfill's suppression.
                        item_metadata.pop("pending_archive_only", None)
                    row.metadata_json = item_metadata
                    if changed:
                        source_changed += 1
                        if raw_store is not None and item.raw_snapshot:
                            try:
                                version = current_item_version(session, row)
                                version.raw_storage_path = raw_store.put(
                                    source_id=spec.id,
                                    item_id=row.id,
                                    content_hash=version.content_hash,
                                    payload=item.raw_snapshot,
                                    fetched_at=version.fetched_at,
                                )
                            except Exception as exc:
                                batch.warnings.append(
                                    f"private raw snapshot upload failed for {row.id}: {exc}"
                                )
                    else:
                        source_unchanged += 1
                health_metadata = dict(health.metadata_json or {})
                previous_empty_streak = int(health_metadata.get("empty_streak", 0))
                if not batch.not_modified and not batch.items:
                    health_metadata["empty_streak"] = previous_empty_streak + 1
                    if not bool(getattr(spec, "allow_empty", False)):
                        batch.warnings.append("source returned zero usable items")
                else:
                    health_metadata["empty_streak"] = 0
                health_metadata["last_item_count"] = len(batch.items)
                health.metadata_json = health_metadata
                _apply_cursor(cursor_row, batch.cursor)
                health.status = "healthy" if not batch.warnings else "degraded"
                health.last_success_at = utcnow()
                health.consecutive_failures = 0
                health.last_http_status = getattr(collector, "last_http_status", None)
                health.last_error = "; ".join(batch.warnings)[:2000] or None
                source_row.next_due_at = utcnow() + _cadence_delta(spec.cadence)
                health.updated_at = utcnow()
            session.commit()
            stats.discovered += source_discovered
            stats.changed += source_changed
            stats.unchanged += source_unchanged
            stats.not_modified += int(batch.not_modified)
            stats.degraded += int(bool(batch.warnings))
        except Exception as exc:
            # A DBAPI disconnect invalidates the entire outer transaction even
            # when the work used a savepoint. Roll it back fully before trying
            # to persist source health on a replacement connection.
            session.rollback()
            http_error = exc if isinstance(exc, CollectorHTTPError) else None
            LOGGER.warning(
                "source collection failed: source_id=%s error_type=%s "
                "connection_invalidated=%s status_code=%s retryable=%s host=%s",
                spec.id,
                type(exc).__name__,
                bool(getattr(exc, "connection_invalidated", False)),
                http_error.status_code if http_error is not None else None,
                http_error.retryable if http_error is not None else False,
                http_error.host if http_error is not None else None,
            )
            stats.failed += 1
            source_row = session.get(SourceModel, spec.id)
            if source_row is None:
                source_row = sync_source(session, spec)
            health = ensure_source_health(session, spec.id)
            health.status = "failing"
            health.consecutive_failures += 1
            health.last_http_status = (
                http_error.status_code if http_error is not None else None
            )
            health.last_error = str(exc)[:2000]
            source_row.next_due_at = utcnow() + timedelta(minutes=30)
            health.updated_at = utcnow()
            session.commit()
        finally:
            collector.close()
    return stats


def _source_authorization(
    kind: str,
    *,
    github_token: str | None,
    openreview_access_token: str | None,
) -> str | None:
    if kind == "github_releases" and github_token:
        return f"Bearer {github_token}"
    if kind == "openreview_api" and openreview_access_token:
        return f"Bearer {openreview_access_token}"
    return None


def _cadence_delta(cadence: str) -> timedelta:
    return {
        "four_hour": timedelta(hours=4),
        "daily": timedelta(days=1),
        "manual": timedelta(days=3650),
    }.get(cadence, timedelta(days=1))


def _cursor_payload(row) -> dict[str, Any]:
    return {
        **(row.cursor or {}),
        **({"etag": row.etag} if row.etag else {}),
        **({"last_modified": row.last_modified} if row.last_modified else {}),
        **(
            {"last_seen_native_id": row.last_seen_native_id}
            if row.last_seen_native_id
            else {}
        ),
    }


def _apply_cursor(row, value: dict[str, Any]) -> None:
    value = dict(value)
    row.etag = value.pop("etag", row.etag)
    row.last_modified = value.pop("last_modified", row.last_modified)
    row.last_seen_native_id = value.pop("last_seen_native_id", row.last_seen_native_id)
    row.cursor = value
    row.updated_at = utcnow()


def verification_for(evidence_type: str) -> VerificationStatus:
    if evidence_type in {
        "paper",
        "regulatory_filing",
        "exchange_filing",
        "official_filing",
        "official_repo",
        "official_standard",
    }:
        return VerificationStatus.VERIFIED_PRIMARY
    if evidence_type == "official_company":
        return VerificationStatus.COMPANY_CLAIM
    if evidence_type == "reputable_media":
        return VerificationStatus.REPORTED_UNCONFIRMED
    return VerificationStatus.REPORTED_UNCONFIRMED


def _pending_archive_only(
    metadata: dict[str, Any], current_content_hash: str
) -> tuple[bool, datetime | None]:
    marker = metadata.get("pending_archive_only")
    if not isinstance(marker, dict) or marker.get("content_hash") != current_content_hash:
        return False, None
    value = marker.get("source_time_cutoff")
    if not isinstance(value, str):
        return True, None
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # The marker still fails closed for delivery even if its optional
        # cutoff cannot be parsed.
        return True, None
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    return True, cutoff


def enrich_pending(
    session: Session,
    *,
    classifier: RuleTopicClassifier,
    qwen: QwenClient | None = None,
    config_dir: Path | str = "configs",
    limit: int = 300,
    timezone: str = "Asia/Shanghai",
    daily_qwen_limit: int = 300,
    suppress_delivery: bool = False,
    source_time_cutoff: datetime | None = None,
) -> dict[str, int]:
    processed_marker = ItemModel.metadata_json["processed_hash"].as_string()
    items = session.scalars(
        select(ItemModel)
        .where(
            or_(
                processed_marker.is_(None),
                processed_marker != ItemModel.current_content_hash,
            )
        )
        .order_by(ItemModel.first_seen_at.desc())
        .limit(limit)
    ).all()
    issuer_names = _issuer_names(config_dir)
    issuer_aliases = _issuer_aliases(config_dir)
    pure_ai_issuers = _pure_ai_issuers(config_dir)
    stats = {
        "processed": 0,
        "published": 0,
        "archived": 0,
        "llm_fallbacks": 0,
        "llm_budget_exhausted": 0,
        "embedding_pending": 0,
    }
    usage_date = utcnow().astimezone(ZoneInfo(timezone)).date()
    for item in items:
        item_meta = dict(item.metadata_json or {})
        pending_archive_only, pending_archive_cutoff = _pending_archive_only(
            item_meta, item.current_content_hash
        )
        if "pending_archive_only" in item_meta and not pending_archive_only:
            item_meta.pop("pending_archive_only", None)
        effective_suppress_delivery = suppress_delivery or pending_archive_only
        effective_source_time_cutoff = source_time_cutoff or pending_archive_cutoff
        if item_meta.get("processed_hash") == item.current_content_hash:
            continue
        if stats["processed"] >= limit:
            break
        version = current_item_version(session, item)
        source = session.get(SourceModel, item.source_id)
        if source is None:
            continue
        source_timestamp = version.source_time or item.published_at
        if source_timestamp is not None and source_timestamp.tzinfo is None:
            source_timestamp = source_timestamp.replace(tzinfo=UTC)
        if effective_source_time_cutoff is not None and (
            source_timestamp is None or source_timestamp < effective_source_time_cutoff
        ):
            item_meta["processed_hash"] = item.current_content_hash
            item_meta["backfill_outside_window"] = True
            item_meta.pop("pending_archive_only", None)
            item.metadata_json = item_meta
            stats["processed"] += 1
            stats["archived"] += 1
            continue
        body = f"{version.abstract_text or ''}\n{version.normalized_text or ''}"
        evidence_type = str(item_meta.get("evidence_type", source.evidence_type))
        if evidence_type == "reputable_media":
            detected_issuer = _detect_issuer(f"{item.title}\n{body}", issuer_aliases)
            if detected_issuer:
                item.entity_id = detected_issuer
        event_type = _event_type_for(item, version, source.kind)
        if (
            event_type in CAPITAL_EVENT_TYPES
            and item.entity_id not in pure_ai_issuers
            and not _capital_ai_relevant(f"{item.title}\n{body}")
        ):
            item_meta["processed_hash"] = item.current_content_hash
            item_meta.pop("pending_archive_only", None)
            item.metadata_json = item_meta
            stats["processed"] += 1
            stats["archived"] += 1
            continue
        match = classifier.classify(
            item.title,
            body,
            event_type=event_type,
            source_id=source.id,
            evidence_type=evidence_type,
        )
        if not match.topics:
            item_meta["processed_hash"] = item.current_content_hash
            item_meta.pop("pending_archive_only", None)
            item.metadata_json = item_meta
            stats["processed"] += 1
            stats["archived"] += 1
            continue

        qwen_for_item = None
        if qwen and qwen.enabled:
            if reserve_daily_usage(
                session,
                usage_date=usage_date,
                usage_key="qwen_flash_classification",
                hard_limit=daily_qwen_limit,
            ):
                qwen_for_item = qwen
            else:
                stats["llm_budget_exhausted"] += 1
        enhanced = (
            qwen_for_item.enhance(_as_collected(item, version), match.topics, summarize=False)
            if qwen_for_item
            else None
        )
        if qwen_for_item and enhanced is None:
            stats["llm_fallbacks"] += 1
        topics = enhanced.topics if enhanced and enhanced.topics else match.topics
        verification = verification_for(str(item_meta.get("evidence_type", source.evidence_type)))
        status = EventStatus(item_meta.get("update_status", EventStatus.NEW_ENTITY.value))
        embedding_input = f"{item.title}\n{body}"
        if qwen is not None:
            embedded = qwen.embed_with_provenance(embedding_input)
            embedding = embedded.vector
            embedding_space = embedded.space
        else:
            embedding = deterministic_embedding(embedding_input)
            embedding_space = "feature-hash-v1"
        version.embedding = embedding
        version.embedding_vector = embedding
        version.metadata_json = {
            **(version.metadata_json or {}),
            "embedding_space": embedding_space,
            "embedding_pending": bool(
                qwen is not None
                and getattr(qwen, "remote_embedding_enabled", qwen.enabled)
                and embedding_space == "feature-hash-v1"
            ),
        }
        embedding_pending = bool(version.metadata_json["embedding_pending"])
        stats["embedding_pending"] += int(embedding_pending)
        event_id = stable_id("event", item.id)
        existing = session.get(RadarEventModel, event_id)
        if existing is not None:
            cluster_id = existing.cluster_id
            duplicate = existing.cluster_id != existing.id
            canonical_event_id = existing.cluster_id if duplicate else None
        else:
            cluster_id, duplicate, canonical_event_id = _choose_cluster(
                session,
                item=item,
                event_id=event_id,
                event_type=event_type,
                embedding=embedding,
                embedding_space=embedding_space,
                qwen=qwen_for_item,
                item_text=f"{item.title}\n{body}",
            )
        strength = max((match.strengths.get(topic.value, 0) for topic in topics), default=0)
        routine_record = bool(version.metadata_json.get("routine"))
        breakdown = score_event(
            topic_strength=strength,
            evidence_type=str(item_meta.get("evidence_type", source.evidence_type)),
            status=status,
            verification=verification,
            event_type=event_type,
            is_duplicate=duplicate,
            is_ordinary_commit=routine_record,
        )
        now = utcnow()
        previous_public = bool(existing.is_public) if existing is not None else False
        previous_score = int(existing.score) if existing is not None else 0
        previous_material_at = existing.material_updated_at if existing is not None else None
        previous_verification = (
            VerificationStatus(existing.verification_status) if existing is not None else None
        )
        preserve_cluster_primary = bool(
            existing is not None
            and previous_verification is not None
            and _verification_rank(previous_verification) > _verification_rank(verification)
            and canonicalize_url(existing.primary_url) != canonicalize_url(item.canonical_url)
        )
        previous_cluster_fields = (
            {
                "primary_url": existing.primary_url,
                "source_type": existing.source_type,
                "title_zh": existing.title_zh,
                "summary_zh": existing.summary_zh,
                "why_it_matters": existing.why_it_matters,
                "source_time": existing.source_time,
                "corroborating_urls": list(existing.corroborating_urls or []),
                "topics": list(existing.topics or []),
                "cross_tags": list(existing.cross_tags or []),
            }
            if existing is not None
            else None
        )
        event = existing or RadarEventModel(id=event_id, cluster_id=cluster_id, created_at=now)
        event.cluster_id = cluster_id
        event.event_type = event_type
        event.topics = [topic.value for topic in topics]
        if source.kind in {"arxiv_api", "arxiv_oai"}:
            event.entities = [
                f"author:{name}"
                for name in (version.metadata_json or {}).get("authors", [])[:3]
                if name
            ]
        else:
            event.entities = [item.entity_id] if item.entity_id else []
        event.cross_tags = match.cross_tags
        event.title_zh = normalize_content(enhanced.title_zh if enhanced and enhanced.title_zh else item.title)
        event.summary_zh = normalize_content(
            enhanced.summary_zh if enhanced and enhanced.summary_zh else (version.abstract_text or item.title)[:1200]
        )
        event.why_it_matters = normalize_content(
            enhanced.why_it_matters
            if enhanced and enhanced.why_it_matters
            else _fallback_why(topics, event_type)
        )
        event.change_summary = str(item_meta.get("change_summary") or change_summary(status))
        event.source_time = version.source_time or item.published_at
        event.first_seen_at = existing.first_seen_at if existing else item.first_seen_at
        event.material_updated_at = (
            now if status == EventStatus.MATERIAL_UPDATE else previous_material_at
        )
        event.status = status.value
        event.source_type = source.kind
        cluster_verification = (
            _combined_verification(previous_verification, verification)
            if previous_verification is not None
            else verification
        )
        event.verification_status = cluster_verification.value
        event.score = (
            max(previous_score, breakdown.total)
            if status == EventStatus.MINOR_UPDATE
            else breakdown.total
        )
        event.primary_url = item.canonical_url
        event.corroborating_urls = (
            list(previous_cluster_fields["corroborating_urls"])
            if previous_cluster_fields
            else []
        )
        event.is_public = previous_public if status == EventStatus.MINOR_UPDATE else (
            not duplicate
            and not routine_record
            and max(previous_score, breakdown.total) >= 45
            and cluster_verification != VerificationStatus.REPORTED_UNCONFIRMED
        )
        if preserve_cluster_primary and previous_cluster_fields:
            event.primary_url = previous_cluster_fields["primary_url"]
            event.source_type = previous_cluster_fields["source_type"]
            event.title_zh = previous_cluster_fields["title_zh"]
            event.summary_zh = previous_cluster_fields["summary_zh"]
            event.why_it_matters = previous_cluster_fields["why_it_matters"]
            event.source_time = previous_cluster_fields["source_time"]
            event.topics = list(
                dict.fromkeys([*previous_cluster_fields["topics"], *event.topics])
            )
            event.cross_tags = list(
                dict.fromkeys([*previous_cluster_fields["cross_tags"], *event.cross_tags])
            )
            event.score = max(previous_score, event.score)
            event.is_public = previous_public
        was_suppressed_before_embedding = bool(event.delivery_suppressed)
        archive_marker_before = bool(
            item_meta.get("archive_delivery_suppressed", False)
        )
        release_archive_on_material = bool(
            not effective_suppress_delivery and status != EventStatus.MINOR_UPDATE
        )
        if release_archive_on_material:
            item_meta.pop("archive_delivery_suppressed", None)
        if embedding_pending:
            # Do not publish a second card across a temporary vector-space
            # outage. Recovery re-embeds and re-clusters before releasing it.
            event.is_public = False
            event.delivery_suppressed = True
            item_meta["embedding_delivery_suppressed"] = True
            item_meta["embedding_previous_delivery_suppressed"] = (
                was_suppressed_before_embedding
                and not (archive_marker_before and release_archive_on_material)
            )
            if effective_suppress_delivery:
                item_meta["archive_delivery_suppressed"] = True
        elif effective_suppress_delivery:
            event.delivery_suppressed = True
            item_meta["archive_delivery_suppressed"] = True
        elif status != EventStatus.MINOR_UPDATE:
            event.delivery_suppressed = False
            item_meta.pop("archive_delivery_suppressed", None)
        event.updated_at = now
        if existing is None:
            session.add(event)
        session.flush()

        revision_no = _next_event_revision_no(session, event.id)
        snapshot_version = (
            _primary_event_version(session, event.id) or version
            if preserve_cluster_primary
            else version
        )
        radar = _to_contract(event, snapshot_version)
        revision_id = stable_id(event.id, str(revision_no), item.current_content_hash)
        revision = session.get(EventRevisionModel, revision_id)
        if revision is None:
            revision = session.scalar(
                select(EventRevisionModel).where(
                    EventRevisionModel.event_id == event.id,
                    or_(
                        EventRevisionModel.revision_no == revision_no,
                        EventRevisionModel.content_hash == item.current_content_hash,
                    ),
                )
            )
        if revision is None:
            revision = EventRevisionModel(
                id=revision_id,
                event_id=event.id,
                revision_no=revision_no,
                content_hash=item.current_content_hash,
                status=status.value,
                is_material=status
                in {
                    EventStatus.NEW_ENTITY,
                    EventStatus.MATERIAL_UPDATE,
                    EventStatus.DISCOVERED_LATE,
                },
                snapshot=radar.model_dump(mode="json"),
            )
            session.add(revision)
        else:
            revision.status = status.value
            revision.is_material = status in {
                EventStatus.NEW_ENTITY,
                EventStatus.MATERIAL_UPDATE,
                EventStatus.DISCOVERED_LATE,
            }
            revision.snapshot = radar.model_dump(mode="json")
        if session.get(EventItemModel, (event.id, version.id)) is None:
            session.add(
                EventItemModel(
                    event_id=event.id,
                    item_version_id=version.id,
                    relation="supports" if preserve_cluster_primary else "primary",
                )
            )
        if canonical_event_id:
            canonical = session.get(RadarEventModel, canonical_event_id)
            if canonical is not None:
                links = list(canonical.corroborating_urls or [])
                added_link = False
                same_document = canonicalize_url(canonical.primary_url) == canonicalize_url(
                    item.canonical_url
                )
                if not same_document and not any(
                    canonicalize_url(str(link.get("url", "")))
                    == canonicalize_url(item.canonical_url)
                    for link in links
                    if link.get("url")
                ):
                    links.append({"label": item.title[:120], "url": item.canonical_url})
                    canonical.corroborating_urls = links
                    canonical.updated_at = now
                    added_link = True
                if session.get(EventItemModel, (canonical.id, version.id)) is None:
                    session.add(
                        EventItemModel(
                            event_id=canonical.id,
                            item_version_id=version.id,
                            relation="supports",
                        )
                    )
                known_cluster_member = bool(
                    existing is not None and existing.cluster_id == canonical.id
                )
                if (
                    (not same_document and (added_link or status == EventStatus.MATERIAL_UPDATE))
                    or (
                        known_cluster_member
                        and status == EventStatus.MATERIAL_UPDATE
                    )
                ):
                    _upgrade_canonical_evidence(
                        session,
                        canonical=canonical,
                        supporting=event,
                        supporting_version=version,
                        supporting_verification=verification,
                        support_score=score_event(
                            topic_strength=strength,
                            evidence_type=str(
                                item_meta.get("evidence_type", source.evidence_type)
                            ),
                            status=status,
                            verification=verification,
                            event_type=event_type,
                            is_duplicate=False,
                            is_ordinary_commit=routine_record,
                        ).total,
                        now=now,
                        suppress_delivery=effective_suppress_delivery,
                        supporting_material=status == EventStatus.MATERIAL_UPDATE,
                    )
        item_meta["processed_hash"] = item.current_content_hash
        item_meta.pop("pending_archive_only", None)
        item.metadata_json = item_meta
        stats["processed"] += 1
        stats["published"] += int(event.is_public)
    session.flush()
    return stats


def recover_pending_embeddings(
    session: Session,
    *,
    qwen: QwenClient,
    limit: int = 100,
    timezone: str = "Asia/Shanghai",
    daily_limit: int = 100,
) -> dict[str, int]:
    """Re-embed outage fallbacks and reconcile any cross-space duplicate roots."""

    stats = {
        "attempted": 0,
        "reembedded": 0,
        "merged": 0,
        "failed": 0,
        "budget_exhausted": 0,
    }
    if not getattr(qwen, "remote_embedding_enabled", qwen.enabled) or limit <= 0:
        return stats
    pending_marker = ItemVersionModel.metadata_json["embedding_pending"].as_boolean()
    candidates = session.scalars(
        select(ItemVersionModel)
        .join(ItemModel, ItemModel.id == ItemVersionModel.item_id)
        .where(
            ItemVersionModel.content_hash == ItemModel.current_content_hash,
            pending_marker.is_(True),
        )
        .order_by(ItemVersionModel.fetched_at.asc())
        .limit(limit)
    ).all()
    usage_date = utcnow().astimezone(ZoneInfo(timezone)).date()
    for version in candidates:
        if not reserve_daily_usage(
            session,
            usage_date=usage_date,
            usage_key="qwen_embedding_recovery",
            hard_limit=daily_limit,
        ):
            stats["budget_exhausted"] += 1
            break
        stats["attempted"] += 1
        embedded = qwen.embed_with_provenance(
            f"{version.title}\n{version.abstract_text or ''}\n{version.normalized_text or ''}"
        )
        if embedded.space == "feature-hash-v1":
            stats["failed"] += 1
            continue
        version.embedding = embedded.vector
        version.embedding_vector = embedded.vector
        version.metadata_json = {
            **(version.metadata_json or {}),
            "embedding_space": embedded.space,
            "embedding_pending": False,
            "reembedded_at": utcnow().isoformat().replace("+00:00", "Z"),
        }
        stats["reembedded"] += 1
        item = session.get(ItemModel, version.item_id)
        if item is None:
            continue
        event = session.scalar(
            select(RadarEventModel)
            .join(EventItemModel, EventItemModel.event_id == RadarEventModel.id)
            .where(
                EventItemModel.item_version_id == version.id,
                EventItemModel.relation == "primary",
            )
        )
        if event is None:
            continue
        if event.cluster_id != event.id:
            item_meta = dict(item.metadata_json or {})
            item_meta["embedding_delivery_suppressed"] = False
            item.metadata_json = item_meta
            continue
        cluster_id, duplicate, canonical_id = _choose_cluster(
            session,
            item=item,
            event_id=event.id,
            event_type=event.event_type,
            embedding=embedded.vector,
            embedding_space=embedded.space,
            qwen=qwen,
            item_text=(
                f"{item.title}\n{version.abstract_text or ''}\n"
                f"{version.normalized_text or ''}"
            ),
        )
        item_meta = dict(item.metadata_json or {})
        was_embedding_suppressed = bool(item_meta.pop("embedding_delivery_suppressed", False))
        previous_suppressed = bool(
            item_meta.pop("embedding_previous_delivery_suppressed", False)
        )
        archive_suppressed = bool(item_meta.get("archive_delivery_suppressed", False))
        item.metadata_json = item_meta
        if duplicate and canonical_id:
            canonical = session.get(RadarEventModel, canonical_id)
            if canonical is not None:
                event.cluster_id = cluster_id
                event.is_public = False
                event.delivery_suppressed = True
                if session.get(EventItemModel, (canonical.id, version.id)) is None:
                    session.add(
                        EventItemModel(
                            event_id=canonical.id,
                            item_version_id=version.id,
                            relation="supports",
                        )
                    )
                links = list(canonical.corroborating_urls or [])
                if canonicalize_url(canonical.primary_url) != canonicalize_url(item.canonical_url) and not any(
                    canonicalize_url(str(link.get("url") or ""))
                    == canonicalize_url(item.canonical_url)
                    for link in links
                    if link.get("url")
                ):
                    links.append({"label": item.title[:120], "url": item.canonical_url})
                    canonical.corroborating_urls = links
                _upgrade_canonical_evidence(
                    session,
                    canonical=canonical,
                    supporting=event,
                    supporting_version=version,
                    supporting_verification=VerificationStatus(event.verification_status),
                    support_score=event.score,
                    now=utcnow(),
                    suppress_delivery=archive_suppressed or previous_suppressed,
                    supporting_material=event.status == EventStatus.MATERIAL_UPDATE.value,
                )
                stats["merged"] += 1
        elif was_embedding_suppressed:
            event.delivery_suppressed = archive_suppressed or previous_suppressed
            event.is_public = (
                event.score >= 45
                and event.verification_status
                != VerificationStatus.REPORTED_UNCONFIRMED.value
            )
            event.updated_at = utcnow()
    session.flush()
    return stats


def editorialize_top(
    session: Session,
    *,
    qwen: QwenClient,
    limit: int = 20,
    timezone: str = "Asia/Shanghai",
    daily_limit: int = 20,
) -> dict[str, int]:
    """Apply Qwen Plus to the current highest-signal cards, independently of ingestion."""

    stats = {"attempted": 0, "updated": 0, "failed": 0}
    if not qwen.enabled or limit <= 0:
        return stats
    usage_date = utcnow().astimezone(ZoneInfo(timezone)).date()
    events = session.scalars(
        select(RadarEventModel)
        .where(RadarEventModel.is_public.is_(True), RadarEventModel.archived_at.is_(None))
        .order_by(RadarEventModel.score.desc(), RadarEventModel.updated_at.desc())
        .limit(limit * 3)
    ).all()
    for event in events:
        if stats["attempted"] >= limit:
            break
        version = session.scalar(
            select(ItemVersionModel)
            .join(EventItemModel, EventItemModel.item_version_id == ItemVersionModel.id)
            .where(
                EventItemModel.event_id == event.id,
                EventItemModel.relation == "primary",
            )
            .order_by(ItemVersionModel.fetched_at.desc())
        )
        if version is None:
            continue
        metadata = dict(version.metadata_json or {})
        marker = f"{qwen.summarizer_model}:{version.content_hash}"
        if metadata.get("qwen_plus_marker") == marker:
            continue
        if not reserve_daily_usage(
            session,
            usage_date=usage_date,
            usage_key="qwen_plus_editorial",
            hard_limit=daily_limit,
        ):
            break
        topics = [Topic(topic) for topic in event.topics]
        stats["attempted"] += 1
        result = qwen.summarize(_as_collected_from_event(event, version), topics)
        if result is None:
            stats["failed"] += 1
            continue
        event.title_zh = result.title_zh or event.title_zh
        event.summary_zh = result.summary_zh or event.summary_zh
        event.why_it_matters = result.why_it_matters or event.why_it_matters
        event.updated_at = utcnow()
        metadata["qwen_plus_marker"] = marker
        metadata["qwen_plus_enriched_at"] = utcnow().isoformat().replace("+00:00", "Z")
        if result.key_quotes:
            metadata["key_quotes"] = result.key_quotes
        if result.deep_takeaway:
            metadata["deep_takeaway"] = result.deep_takeaway
        version.metadata_json = metadata
        revision = session.scalar(
            select(EventRevisionModel)
            .where(EventRevisionModel.event_id == event.id)
            .order_by(EventRevisionModel.revision_no.desc())
        )
        if revision is not None:
            revision.snapshot = _to_contract(event, version).model_dump(mode="json")
        stats["updated"] += 1
    session.flush()
    return stats


def _choose_cluster(
    session: Session,
    *,
    item: ItemModel,
    event_id: str,
    event_type: str,
    embedding: list[float],
    embedding_space: str,
    qwen: QwenClient | None,
    item_text: str,
) -> tuple[str, bool, str | None]:
    threshold = utcnow() - timedelta(days=14)
    candidate_rows = session.execute(
        _cluster_candidate_query(
            event_id=event_id,
            event_type=event_type,
            threshold=threshold,
        )
    ).all()
    ranked_candidates: list[
        tuple[
            float,
            RadarEventModel,
            str,
            str | None,
            str | None,
        ]
    ] = []
    for (
        candidate,
        linked_title,
        linked_abstract,
        linked_normalized,
        linked_embedding,
        linked_metadata,
    ) in candidate_rows:
        if not linked_embedding:
            continue
        if (linked_metadata or {}).get("embedding_space") != embedding_space:
            continue
        similarity = cosine_similarity(embedding, linked_embedding)
        ranked_candidates.append(
            (
                similarity,
                candidate,
                linked_title,
                linked_abstract,
                linked_normalized,
            )
        )

    # pgvector previously applied this bound while ordering with `<=>`. Keep
    # the same nearest-neighbour budget, but rank the table-owned float4[]
    # values in Python so the runtime role needs no extension-schema access.
    ranked_candidates.sort(key=lambda row: row[0], reverse=True)
    for (
        similarity,
        candidate,
        linked_title,
        linked_abstract,
        linked_normalized,
    ) in ranked_candidates[:80]:
        if (
            event_type != "PAPER"
            and item.entity_id
            and item.entity_id not in (candidate.entities or [])
        ):
            continue
        if event_type == "PAPER" and normalize_content(item.title).casefold() == normalize_content(
            linked_title
        ).casefold():
            similarity = max(similarity, 0.99)
        decision = cluster_decision(
            similarity,
            both_arxiv=(
                item.item_type in {"arxiv_api", "arxiv_oai"}
                and candidate.source_type in {"arxiv_api", "arxiv_oai"}
            ),
            same_arxiv_id=False,
        )
        should_merge = decision == ClusterDecision.MERGE
        if decision == ClusterDecision.LLM_REVIEW and qwen is not None:
            candidate_text = (
                f"{linked_title}\n{linked_abstract or ''}\n{linked_normalized or ''}"
            )
            adjudication = qwen.adjudicate_merge(item_text, candidate_text)
            should_merge = bool(adjudication and adjudication.same_event)
        if should_merge:
            return candidate.cluster_id, True, candidate.id
    return event_id, False, None


def _cluster_candidate_query(
    *,
    event_id: str,
    event_type: str,
    threshold: datetime,
):
    """Return recent root events with their latest primary float-array embedding.

    The query deliberately selects ``item_versions.embedding`` rather than the
    pgvector mirror column. Similarity ordering remains a Python concern, which
    keeps the runtime path independent of operators and functions in the
    PostgreSQL extensions schema.
    """

    eligible_events = (
        select(RadarEventModel.id.label("event_id"))
        .where(
            RadarEventModel.event_type == event_type,
            RadarEventModel.first_seen_at >= threshold,
            RadarEventModel.id != event_id,
            RadarEventModel.cluster_id == RadarEventModel.id,
        )
        .subquery("eligible_cluster_events")
    )
    latest_primary = (
        select(
            EventItemModel.event_id.label("event_id"),
            ItemVersionModel.title.label("title"),
            ItemVersionModel.abstract_text.label("abstract_text"),
            ItemVersionModel.normalized_text.label("normalized_text"),
            ItemVersionModel.embedding.label("embedding"),
            ItemVersionModel.metadata_json.label("metadata"),
            func.row_number()
            .over(
                partition_by=EventItemModel.event_id,
                order_by=(
                    ItemVersionModel.fetched_at.desc(),
                    ItemVersionModel.id.desc(),
                ),
            )
            .label("version_rank"),
        )
        .select_from(EventItemModel)
        .join(
            eligible_events,
            eligible_events.c.event_id == EventItemModel.event_id,
        )
        .join(
            ItemVersionModel,
            ItemVersionModel.id == EventItemModel.item_version_id,
        )
        .where(EventItemModel.relation == "primary")
        .subquery("latest_primary_item_version")
    )
    return (
        select(
            RadarEventModel,
            latest_primary.c.title,
            latest_primary.c.abstract_text,
            latest_primary.c.normalized_text,
            latest_primary.c.embedding,
            latest_primary.c.metadata,
        )
        .join(latest_primary, latest_primary.c.event_id == RadarEventModel.id)
        .where(
            latest_primary.c.version_rank == 1,
            latest_primary.c.embedding.is_not(None),
        )
        .order_by(RadarEventModel.id)
    )


def _event_type_for(item: ItemModel, version: ItemVersionModel, kind: str) -> str:
    metadata = version.metadata_json or {}
    form = str(metadata.get("form", ""))
    if form.startswith(("S-1", "F-1", "424B")):
        return "IPO_FILING"
    if form.startswith(("4", "SC 13D", "SC 13G")):
        return "OWNERSHIP"
    filing_items = str(metadata.get("filing_items", ""))
    if form.startswith(("8-K", "6-K")):
        if any(code in filing_items for code in ("1.01", "1.02")):
            return "MATERIAL_CONTRACT"
        if "2.01" in filing_items:
            return "M_AND_A"
        if "3.02" in filing_items:
            return "RAISE"
    return infer_event_type(item.title, f"{version.abstract_text or ''} {version.normalized_text or ''}", kind=kind)


def _upgrade_canonical_evidence(
    session: Session,
    *,
    canonical: RadarEventModel,
    supporting: RadarEventModel,
    supporting_version: ItemVersionModel,
    supporting_verification: VerificationStatus,
    support_score: int,
    now,
    suppress_delivery: bool = False,
    supporting_material: bool = False,
) -> None:
    """Promote a cluster when independent/primary evidence arrives later."""

    current = VerificationStatus(canonical.verification_status)
    upgraded = _combined_verification(current, supporting_verification)
    two_media_sources = (
        current == VerificationStatus.REPORTED_UNCONFIRMED
        and supporting_verification == VerificationStatus.REPORTED_UNCONFIRMED
        and len(_cluster_media_source_ids(session, canonical.id)) >= 2
    )
    verification_changed = upgraded != current
    primary_arrived = supporting_verification in {
        VerificationStatus.VERIFIED_PRIMARY,
        VerificationStatus.CORROBORATED,
        VerificationStatus.COMPANY_CLAIM,
    } and current in {
        VerificationStatus.COMPANY_CLAIM,
        VerificationStatus.REPORTED_UNCONFIRMED,
    } and _verification_rank(supporting_verification) > _verification_rank(current)
    support_updates_primary = supporting_material and (
        canonical.primary_url == supporting.primary_url or primary_arrived
    )
    if verification_changed:
        canonical.verification_status = upgraded.value
    if primary_arrived or support_updates_primary:
        old_primary = canonical.primary_url
        links = [
            link for link in (canonical.corroborating_urls or []) if link.get("url") != supporting.primary_url
        ]
        if old_primary != supporting.primary_url and not any(
            link.get("url") == old_primary for link in links
        ):
            links.append({"label": "此前线索", "url": old_primary})
        canonical.corroborating_urls = links
        canonical.primary_url = supporting.primary_url
        canonical.source_type = supporting.source_type
        canonical.title_zh = supporting.title_zh
        canonical.summary_zh = supporting.summary_zh
        canonical.why_it_matters = supporting.why_it_matters
        canonical.source_time = supporting.source_time
        for relation in session.scalars(
            select(EventItemModel).where(EventItemModel.event_id == canonical.id)
        ).all():
            relation.relation = (
                "primary"
                if relation.item_version_id == supporting_version.id
                else "supports"
            )
    canonical.topics = list(dict.fromkeys([*(canonical.topics or []), *(supporting.topics or [])]))
    canonical.cross_tags = list(
        dict.fromkeys([*(canonical.cross_tags or []), *(supporting.cross_tags or [])])
    )
    canonical.score = max(canonical.score, support_score)
    canonical.is_public = (
        upgraded != VerificationStatus.REPORTED_UNCONFIRMED and canonical.score >= 45
    )

    if not (
        verification_changed
        or primary_arrived
        or two_media_sources
        or supporting_material
    ):
        return
    canonical.status = EventStatus.MATERIAL_UPDATE.value
    canonical.delivery_suppressed = suppress_delivery
    canonical.material_updated_at = now
    canonical.change_summary = (
        "新增一级来源，已提升验证等级并更新主证据"
        if primary_arrived
        else (
            "已有佐证来源发生实质更新，事件证据链已同步"
            if supporting_material
            else "新增独立佐证来源，事件证据链已更新"
        )
    )
    canonical.updated_at = now
    revision_no = _next_event_revision_no(session, canonical.id)
    digest = content_hash(
        "\n".join(
            [
                canonical.id,
                supporting_version.content_hash,
                canonical.verification_status,
                canonical.primary_url,
                *(link.get("url", "") for link in canonical.corroborating_urls or []),
            ]
        )
    )
    snapshot_version = (
        supporting_version
        if primary_arrived or support_updates_primary
        else (_primary_event_version(session, canonical.id) or supporting_version)
    )
    revision_id = stable_id(canonical.id, "evidence", str(revision_no), digest)
    existing_revision = session.get(EventRevisionModel, revision_id)
    if existing_revision is None:
        existing_revision = session.scalar(
            select(EventRevisionModel).where(
                EventRevisionModel.event_id == canonical.id,
                or_(
                    EventRevisionModel.revision_no == revision_no,
                    EventRevisionModel.content_hash == digest,
                ),
            )
        )
    if existing_revision is None:
        session.add(
            EventRevisionModel(
                id=revision_id,
                event_id=canonical.id,
                revision_no=revision_no,
                content_hash=digest,
                status=EventStatus.MATERIAL_UPDATE.value,
                is_material=True,
                snapshot=_to_contract(canonical, snapshot_version).model_dump(mode="json"),
            )
        )
    else:
        existing_revision.status = EventStatus.MATERIAL_UPDATE.value
        existing_revision.is_material = True
        existing_revision.snapshot = _to_contract(canonical, snapshot_version).model_dump(mode="json")


def _cluster_media_source_ids(session: Session, event_id: str) -> set[str]:
    return set(
        session.scalars(
            select(SourceModel.id)
            .join(ItemModel, ItemModel.source_id == SourceModel.id)
            .join(ItemVersionModel, ItemVersionModel.item_id == ItemModel.id)
            .join(EventItemModel, EventItemModel.item_version_id == ItemVersionModel.id)
            .where(
                EventItemModel.event_id == event_id,
                SourceModel.evidence_type == "reputable_media",
            )
        ).all()
    )


def _combined_verification(
    current: VerificationStatus, supporting: VerificationStatus
) -> VerificationStatus:
    if VerificationStatus.CORROBORATED in {current, supporting}:
        return VerificationStatus.CORROBORATED
    if current == supporting == VerificationStatus.REPORTED_UNCONFIRMED:
        return VerificationStatus.REPORTED_UNCONFIRMED
    if current == supporting == VerificationStatus.COMPANY_CLAIM:
        return VerificationStatus.COMPANY_CLAIM
    if VerificationStatus.VERIFIED_PRIMARY in {current, supporting}:
        return VerificationStatus.CORROBORATED
    if VerificationStatus.COMPANY_CLAIM in {current, supporting}:
        return VerificationStatus.COMPANY_CLAIM
    return current


def _verification_rank(value: VerificationStatus) -> int:
    return {
        VerificationStatus.REPORTED_UNCONFIRMED: 1,
        VerificationStatus.COMPANY_CLAIM: 2,
        VerificationStatus.VERIFIED_PRIMARY: 3,
        VerificationStatus.CORROBORATED: 4,
    }[value]


def _next_event_revision_no(session: Session, event_id: str) -> int:
    previous = session.scalar(
        select(EventRevisionModel)
        .where(EventRevisionModel.event_id == event_id)
        .order_by(EventRevisionModel.revision_no.desc())
    )
    return (previous.revision_no if previous else 0) + 1


def _primary_event_version(session: Session, event_id: str) -> ItemVersionModel | None:
    return session.scalar(
        select(ItemVersionModel)
        .join(EventItemModel, EventItemModel.item_version_id == ItemVersionModel.id)
        .where(
            EventItemModel.event_id == event_id,
            EventItemModel.relation == "primary",
        )
        .order_by(ItemVersionModel.fetched_at.desc())
    )


def _as_collected(item: ItemModel, version: ItemVersionModel):
    from .contracts import CollectedItem

    return CollectedItem(
        source_id=item.source_id,
        external_id=item.native_id or item.id,
        canonical_url=item.canonical_url,
        title=item.title,
        summary=version.abstract_text or "",
        content=version.normalized_text or "",
        published_at=item.published_at,
        updated_at=item.source_updated_at,
        entity_id=item.entity_id,
        evidence_type=str((item.metadata_json or {}).get("evidence_type", "unknown")),
        metadata=version.metadata_json or {},
    )


def _as_collected_from_event(event: RadarEventModel, version: ItemVersionModel):
    from .contracts import CollectedItem

    metadata = version.metadata_json or {}
    insight = metadata.get("alphaxiv_insight") or {}
    insight_text = " ".join(
        [
            str(insight.get("summary") or ""),
            *[str(value) for value in insight.get("key_findings", [])[:5]],
        ]
    ).strip()
    return CollectedItem(
        source_id=event.source_type,
        external_id=event.id,
        canonical_url=event.primary_url,
        title=version.title,
        summary=version.abstract_text or "",
        content="\n".join(
            value
            for value in (version.normalized_text or "", f"alphaXiv deep read: {insight_text}")
            if value
        ),
        published_at=event.source_time,
        entity_id=event.entities[0] if event.entities else None,
        evidence_type=event.source_type,
        metadata=metadata,
    )


def _to_contract(event: RadarEventModel, version: ItemVersionModel) -> RadarEvent:
    metadata = version.metadata_json or {}
    return RadarEvent(
        event_id=event.id,
        cluster_id=event.cluster_id,
        event_type=event.event_type,
        topics=event.topics,
        entities=event.entities,
        title_zh=event.title_zh,
        summary_zh=event.summary_zh,
        why_it_matters=event.why_it_matters,
        source_time=event.source_time,
        first_seen_at=event.first_seen_at,
        material_updated_at=event.material_updated_at,
        status=event.status,
        source_type=event.source_type,
        evidence_type=str(metadata.get("evidence_type", "unknown")),
        verification_status=event.verification_status,
        score=event.score,
        primary_url=event.primary_url,
        corroborating_urls=[link["url"] for link in (event.corroborating_urls or [])],
        cross_tags=event.cross_tags,
        change_summary=event.change_summary or "",
        arxiv_url=metadata.get("arxiv_url"),
        alphaxiv_url=metadata.get("alphaxiv_url"),
        code_url=metadata.get("code_url"),
        project_url=metadata.get("project_url"),
    )


def _fallback_why(topics: list[Topic], event_type: str) -> str:
    labels = "、".join(topic.value for topic in topics)
    return f"该{event_type}事件直接更新了 {labels} 主题的可验证信息。"


def _issuer_names(config_dir: Path | str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for issuer in load_issuers(config_dir):
            result[issuer["id"]] = issuer.get("name_zh") or issuer.get("name_en") or issuer["id"]
    except (FileNotFoundError, ValueError):
        pass
    return result


def _issuer_aliases(config_dir: Path | str) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    try:
        for issuer in load_issuers(config_dir):
            values = {
                issuer.get("name_zh"),
                issuer.get("name_en"),
                *issuer.get("aliases", []),
            }
            for value in values:
                normalized = normalize_content(str(value or "")).casefold()
                if len(normalized) >= 2:
                    aliases.append((normalized, issuer["id"]))
    except (FileNotFoundError, ValueError):
        return []
    return sorted(set(aliases), key=lambda pair: len(pair[0]), reverse=True)


def _detect_issuer(text_value: str, aliases: list[tuple[str, str]]) -> str | None:
    haystack = normalize_content(text_value).casefold()
    return next((issuer_id for alias, issuer_id in aliases if alias in haystack), None)


def _pure_ai_issuers(config_dir: Path | str) -> set[str]:
    try:
        return {
            issuer["id"]
            for issuer in load_issuers(config_dir)
            if issuer.get("priority") == "pure_ai"
        }
    except (FileNotFoundError, ValueError):
        return set()


def _capital_ai_relevant(text: str) -> bool:
    value = text.casefold()
    return any(
        term in value
        for term in (
            "artificial intelligence",
            "generative ai",
            "foundation model",
            "large language model",
            "gpu",
            "accelerator",
            "data center",
            "datacenter",
            "cloud infrastructure",
            "ai model",
            "人工智能",
            "大模型",
            "生成式 ai",
            "算力",
            "智算",
        )
    )
