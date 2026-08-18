-- AI Research Radar initial schema.
-- The database is intentionally private: only the service role can read/write
-- operational tables. Public web data is produced by the sanitized export view
-- and published as static files by GitHub Actions.

create extension if not exists pgcrypto;
create schema if not exists extensions;

-- pgvector is available on hosted Supabase, but local/minimal Postgres installs
-- can still run the rest of this migration. The portable embedding column is a
-- float4[]; an indexed vector(1024) mirror is added below when pgvector exists.
do $$
begin
  begin
    create extension if not exists vector with schema extensions;
  exception
    when undefined_file or insufficient_privilege then
      raise notice 'pgvector is unavailable; continuing with float4[] embeddings';
  end;
end
$$;

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table public.issuer_master (
  id text primary key,
  name_zh text,
  name_en text,
  aliases text[] not null default '{}',
  markets jsonb not null default '[]'::jsonb,
  cik text,
  ir_url text,
  is_private boolean not null default false,
  priority text not null default 'extended'
    check (priority in ('pure_ai', 'platform_compute', 'extended')),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (name_zh is not null or name_en is not null),
  check (jsonb_typeof(markets) = 'array'),
  check (jsonb_typeof(metadata) = 'object')
);

create table public.sources (
  id text primary key,
  entity_id text not null,
  group_name text not null
    check (group_name in ('papers', 'tech', 'capital', 'standards')),
  kind text not null,
  url text not null,
  fetch_strategy text not null,
  cadence text not null check (cadence in ('four_hour', 'daily', 'manual')),
  evidence_type text not null,
  cursor_strategy text,
  parser text not null,
  enabled boolean not null default true,
  config jsonb not null default '{}'::jsonb,
  next_due_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (url ~ '^https?://'),
  check (jsonb_typeof(config) = 'object')
);

create table public.source_cursors (
  source_id text primary key references public.sources(id) on delete cascade,
  cursor jsonb not null default '{}'::jsonb,
  etag text,
  last_modified text,
  last_seen_native_id text,
  watermark_at timestamptz,
  updated_at timestamptz not null default now(),
  check (jsonb_typeof(cursor) = 'object')
);

create table public.ingestion_runs (
  id uuid primary key default gen_random_uuid(),
  group_name text not null
    check (group_name in ('papers', 'tech', 'capital', 'standards', 'all')),
  trigger_kind text not null default 'scheduled'
    check (trigger_kind in ('scheduled', 'manual', 'backfill', 'reconcile')),
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'partial', 'failed')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  discovered_count integer not null default 0 check (discovered_count >= 0),
  changed_count integer not null default 0 check (changed_count >= 0),
  error_count integer not null default 0 check (error_count >= 0),
  metadata jsonb not null default '{}'::jsonb,
  check (finished_at is null or finished_at >= started_at),
  check (jsonb_typeof(metadata) = 'object')
);

create table public.items (
  id text primary key,
  source_id text not null references public.sources(id) on delete restrict,
  native_id text,
  canonical_url text not null,
  item_type text not null,
  entity_id text,
  title text not null,
  published_at timestamptz,
  source_updated_at timestamptz,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  current_content_hash text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (canonical_url ~ '^https?://'),
  check (current_content_hash ~ '^[a-f0-9]{64}$'),
  check (jsonb_typeof(metadata) = 'object')
);

create unique index items_source_native_id_uidx
  on public.items (source_id, native_id)
  where native_id is not null;
create unique index items_source_url_uidx
  on public.items (source_id, canonical_url);
create index items_published_at_idx on public.items (published_at desc);
create index items_entity_id_idx on public.items (entity_id);

create table public.item_versions (
  id text primary key,
  item_id text not null references public.items(id) on delete cascade,
  version_key text not null,
  content_hash text not null,
  title text not null,
  abstract_text text,
  normalized_text text,
  raw_storage_path text,
  source_time timestamptz,
  fetched_at timestamptz not null default now(),
  embedding float4[],
  metadata jsonb not null default '{}'::jsonb,
  unique (item_id, version_key),
  unique (item_id, content_hash),
  check (content_hash ~ '^[a-f0-9]{64}$'),
  check (embedding is null or cardinality(embedding) = 1024),
  check (jsonb_typeof(metadata) = 'object')
);

create index item_versions_fetched_at_idx
  on public.item_versions (fetched_at desc);

-- Add the native pgvector mirror and HNSW index only when the extension loaded.
do $$
declare
  vector_schema text;
begin
  select n.nspname
    into vector_schema
    from pg_extension e
    join pg_namespace n on n.oid = e.extnamespace
   where e.extname = 'vector';

  if vector_schema is not null then
    execute format(
      'alter table public.item_versions add column embedding_vector %I.vector(1024)',
      vector_schema
    );
    begin
      execute format(
        'create index item_versions_embedding_hnsw_idx on public.item_versions using hnsw (embedding_vector %I.vector_cosine_ops)',
        vector_schema
      );
    exception
      when undefined_object or feature_not_supported then
        raise notice 'HNSW is unavailable; embedding_vector remains usable without an ANN index';
    end;
  end if;
end
$$;

create table public.events (
  id text primary key,
  cluster_id text not null,
  event_type text not null,
  topics text[] not null default '{}',
  entities text[] not null default '{}',
  cross_tags text[] not null default '{}',
  title_zh text not null,
  summary_zh text not null,
  why_it_matters text not null,
  change_summary text,
  source_time timestamptz,
  first_seen_at timestamptz not null default now(),
  material_updated_at timestamptz,
  status text not null
    check (status in ('NEW_ENTITY', 'MATERIAL_UPDATE', 'MINOR_UPDATE', 'DISCOVERED_LATE')),
  source_type text not null,
  verification_status text not null
    check (verification_status in ('verified_primary', 'corroborated', 'company_claim', 'reported_unconfirmed')),
  score smallint not null check (score between 0 and 100),
  primary_url text not null,
  corroborating_urls jsonb not null default '[]'::jsonb,
  is_public boolean not null default false,
  delivery_suppressed boolean not null default false,
  archived_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (cardinality(topics) > 0),
  check (primary_url ~ '^https?://'),
  check (jsonb_typeof(corroborating_urls) = 'array')
);

create index events_cluster_id_idx on public.events (cluster_id);
create index events_first_seen_idx on public.events (first_seen_at desc);
create index events_material_updated_idx on public.events (material_updated_at desc);
create index events_topics_gin_idx on public.events using gin (topics);
create index events_entities_gin_idx on public.events using gin (entities);
create index events_public_idx on public.events (first_seen_at desc)
  where is_public and archived_at is null;

create table public.event_revisions (
  id text primary key,
  event_id text not null references public.events(id) on delete cascade,
  revision_no integer not null check (revision_no > 0),
  content_hash text not null,
  status text not null
    check (status in ('NEW_ENTITY', 'MATERIAL_UPDATE', 'MINOR_UPDATE', 'DISCOVERED_LATE')),
  is_material boolean not null default false,
  snapshot jsonb not null,
  created_at timestamptz not null default now(),
  unique (event_id, revision_no),
  unique (event_id, content_hash),
  check (content_hash ~ '^[a-f0-9]{64}$'),
  check (jsonb_typeof(snapshot) = 'object')
);

create table public.event_items (
  event_id text not null references public.events(id) on delete cascade,
  item_version_id text not null references public.item_versions(id) on delete cascade,
  relation text not null default 'supports'
    check (relation in ('primary', 'supports', 'contradicts', 'discovery')),
  created_at timestamptz not null default now(),
  primary key (event_id, item_version_id)
);

create table public.evidence (
  id uuid primary key default gen_random_uuid(),
  event_id text not null references public.events(id) on delete cascade,
  event_revision_id text references public.event_revisions(id) on delete cascade,
  item_version_id text references public.item_versions(id) on delete set null,
  evidence_type text not null,
  verification_status text not null
    check (verification_status in ('verified_primary', 'corroborated', 'company_claim', 'reported_unconfirmed')),
  url text not null,
  publisher text,
  is_primary boolean not null default false,
  occurred_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (url ~ '^https?://'),
  check (jsonb_typeof(metadata) = 'object')
);

create index evidence_event_id_idx on public.evidence (event_id);
create index evidence_revision_id_idx on public.evidence (event_revision_id);

create table public.deliveries (
  delivery_key text primary key,
  recipient_hash text not null,
  channel text not null default 'agentmail' check (channel = 'agentmail'),
  delivery_kind text not null check (delivery_kind in ('digest', 'alert', 'operations')),
  send_at timestamptz,
  state text not null default 'pending'
    check (state in ('pending', 'composed', 'shadow', 'draft', 'scheduled', 'sending', 'unknown', 'sent', 'delivered', 'bounced', 'rejected', 'complained', 'failed', 'cancelled')),
  agentmail_draft_id text,
  agentmail_message_id text,
  delivered_at timestamptz,
  last_error text,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (agentmail_draft_id),
  unique (agentmail_message_id),
  check (recipient_hash ~ '^[a-f0-9]{64}$'),
  check (jsonb_typeof(metadata) = 'object')
);

create table public.delivery_event_revisions (
  delivery_key text not null references public.deliveries(delivery_key) on delete cascade,
  event_revision_id text not null references public.event_revisions(id) on delete restrict,
  created_at timestamptz not null default now(),
  primary key (delivery_key, event_revision_id)
);

create index deliveries_state_send_at_idx
  on public.deliveries (state, send_at);

create table public.webhook_events (
  provider_event_id text primary key,
  provider text not null default 'agentmail' check (provider = 'agentmail'),
  event_type text not null,
  message_id text,
  delivery_key text references public.deliveries(delivery_key) on delete set null,
  signature_verified boolean not null,
  payload jsonb not null,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  processing_error text,
  check (jsonb_typeof(payload) = 'object')
);

create index webhook_events_message_id_idx
  on public.webhook_events (message_id);
create index webhook_events_unprocessed_idx
  on public.webhook_events (received_at)
  where processed_at is null;

create table public.source_health (
  source_id text primary key references public.sources(id) on delete cascade,
  status text not null default 'unknown'
    check (status in ('unknown', 'healthy', 'degraded', 'failing', 'disabled')),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
  last_http_status integer,
  last_latency_ms integer check (last_latency_ms is null or last_latency_ms >= 0),
  last_error text,
  metadata jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  check (last_http_status is null or last_http_status between 100 and 599),
  check (jsonb_typeof(metadata) = 'object')
);

create table public.usage_ledger (
  usage_date date not null,
  usage_key text not null,
  used integer not null default 0 check (used >= 0),
  hard_limit integer not null check (hard_limit > 0),
  updated_at timestamptz not null default now(),
  primary key (usage_date, usage_key),
  check (used <= hard_limit)
);

create trigger issuer_master_touch_updated_at
before update on public.issuer_master
for each row execute function public.touch_updated_at();
create trigger sources_touch_updated_at
before update on public.sources
for each row execute function public.touch_updated_at();
create trigger source_cursors_touch_updated_at
before update on public.source_cursors
for each row execute function public.touch_updated_at();
create trigger items_touch_updated_at
before update on public.items
for each row execute function public.touch_updated_at();
create trigger events_touch_updated_at
before update on public.events
for each row execute function public.touch_updated_at();
create trigger deliveries_touch_updated_at
before update on public.deliveries
for each row execute function public.touch_updated_at();
create trigger source_health_touch_updated_at
before update on public.source_health
for each row execute function public.touch_updated_at();

-- Transactional, idempotent webhook application. State rank prevents a late
-- message.sent event from regressing a delivery already marked delivered.
create or replace function public.apply_agentmail_webhook(
  p_provider_event_id text,
  p_event_type text,
  p_message_id text,
  p_payload jsonb,
  p_signature_verified boolean
)
returns table (was_applied boolean, matched_delivery_key text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  inserted_count integer;
  target_state text;
  target_rank integer;
begin
  if not p_signature_verified then
    raise exception 'webhook signature has not been verified';
  end if;

  insert into public.webhook_events (
    provider_event_id, event_type, message_id, signature_verified, payload
  ) values (
    p_provider_event_id, p_event_type, p_message_id, true, p_payload
  ) on conflict (provider_event_id) do nothing;

  get diagnostics inserted_count = row_count;
  if inserted_count = 0 then
    return query
      select false, w.delivery_key
        from public.webhook_events w
       where w.provider_event_id = p_provider_event_id;
    return;
  end if;

  target_state := case p_event_type
    when 'message.sent' then 'sent'
    when 'message.delivered' then 'delivered'
    when 'message.bounced' then 'bounced'
    when 'message.rejected' then 'rejected'
    when 'message.complained' then 'complained'
    else null
  end;

  target_rank := case target_state
    when 'sent' then 30
    when 'delivered' then 40
    when 'bounced' then 50
    when 'rejected' then 50
    when 'complained' then 60
    else -1
  end;

  if p_message_id is not null and target_state is not null then
    update public.deliveries d
       set state = target_state,
           delivered_at = case
             when target_state = 'delivered' then coalesce(d.delivered_at, now())
             else d.delivered_at
           end,
           last_error = case
             when target_state in ('bounced', 'rejected', 'complained')
               then coalesce(
                 p_payload#>>'{reject,reason}',
                 p_payload#>>'{bounce,reason}',
                 p_payload#>>'{complaint,reason}',
                 p_payload->>'reason',
                 p_payload->>'error',
                 d.last_error
               )
             else d.last_error
           end
     where d.agentmail_message_id = p_message_id
       and target_rank >= case d.state
         when 'pending' then 0
         when 'composed' then 1
         when 'shadow' then 2
         when 'draft' then 5
         when 'scheduled' then 10
         when 'sending' then 20
         when 'unknown' then 20
         when 'sent' then 30
         when 'delivered' then 40
         when 'bounced' then 50
         when 'rejected' then 50
         when 'complained' then 60
         else 100
       end
    returning d.delivery_key into matched_delivery_key;
  end if;

  if matched_delivery_key is null and p_message_id is not null then
    select d.delivery_key into matched_delivery_key
      from public.deliveries d
     where d.agentmail_message_id = p_message_id;
  end if;

  update public.webhook_events
     set delivery_key = matched_delivery_key,
         processed_at = now()
   where provider_event_id = p_provider_event_id;

  return query select true, matched_delivery_key;
end;
$$;

-- The view contains only fields permitted in the public static archive. It is
-- deliberately not granted to anon/authenticated; the CI exporter reads it via
-- service role and writes immutable JSON for GitHub Pages.
create view public.radar_events_export
with (security_invoker = true)
as
select
  e.id as event_id,
  e.cluster_id,
  e.event_type,
  e.topics,
  e.entities,
  e.cross_tags,
  e.title_zh,
  e.summary_zh,
  e.why_it_matters,
  e.change_summary,
  e.source_time,
  e.first_seen_at,
  e.material_updated_at,
  e.status,
  e.source_type,
  e.verification_status,
  e.score,
  e.primary_url,
  e.corroborating_urls,
  e.updated_at
from public.events e
where e.is_public
  and e.archived_at is null
  and e.verification_status <> 'reported_unconfirmed';

-- Every operational table is RLS-protected and has no client policy. The
-- service role bypasses RLS; static-site users never receive a database key.
alter table public.issuer_master enable row level security;
alter table public.sources enable row level security;
alter table public.source_cursors enable row level security;
alter table public.ingestion_runs enable row level security;
alter table public.items enable row level security;
alter table public.item_versions enable row level security;
alter table public.events enable row level security;
alter table public.event_revisions enable row level security;
alter table public.event_items enable row level security;
alter table public.evidence enable row level security;
alter table public.deliveries enable row level security;
alter table public.delivery_event_revisions enable row level security;
alter table public.webhook_events enable row level security;
alter table public.source_health enable row level security;
alter table public.usage_ledger enable row level security;

revoke all on all tables in schema public from anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;
revoke execute on function public.apply_agentmail_webhook(text, text, text, jsonb, boolean)
  from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant usage on schema public to service_role';
    execute 'grant select, insert, update, delete on all tables in schema public to service_role';
    execute 'grant execute on function public.apply_agentmail_webhook(text, text, text, jsonb, boolean) to service_role';
    execute 'grant select on public.radar_events_export to service_role';
  end if;
end
$$;

-- Raw snapshots are private and expire after 14 days through application
-- maintenance. The bucket creation is skipped on plain Postgres without the
-- Supabase Storage schema.
do $$
begin
  if to_regclass('storage.buckets') is not null then
    insert into storage.buckets (id, name, public, file_size_limit)
    values ('radar-raw', 'radar-raw', false, 5242880)
    on conflict (id) do update set public = false, file_size_limit = 5242880;
  end if;
end
$$;
