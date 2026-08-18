-- Dedicated non-owner role for the scheduled GitHub Actions runtime.
-- Its password is generated out of band and stored only as a GitHub Actions secret.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'radar_runtime') then
    create role radar_runtime
      login
      nosuperuser
      nocreatedb
      nocreaterole
      noinherit
      nobypassrls
      connection limit 8;
  else
    alter role radar_runtime
      login
      nosuperuser
      nocreatedb
      nocreaterole
      noinherit
      nobypassrls
      connection limit 8;
  end if;
end
$$;

alter role radar_runtime set search_path = public, extensions;
alter role radar_runtime set statement_timeout = '20min';
alter role radar_runtime set idle_in_transaction_session_timeout = '2min';

grant connect on database postgres to radar_runtime;
grant usage on schema public to radar_runtime;
grant usage on schema extensions to radar_runtime;

grant select, insert, update, delete on
  public.issuer_master,
  public.sources,
  public.source_cursors,
  public.ingestion_runs,
  public.items,
  public.item_versions,
  public.events,
  public.event_revisions,
  public.event_items,
  public.evidence,
  public.deliveries,
  public.delivery_event_revisions,
  public.webhook_events,
  public.source_health,
  public.usage_ledger
to radar_runtime;

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'issuer_master',
    'sources',
    'source_cursors',
    'ingestion_runs',
    'items',
    'item_versions',
    'events',
    'event_revisions',
    'event_items',
    'evidence',
    'deliveries',
    'delivery_event_revisions',
    'webhook_events',
    'source_health',
    'usage_ledger'
  ]
  loop
    execute format(
      'drop policy if exists radar_runtime_all on public.%I',
      table_name
    );
    execute format(
      'create policy radar_runtime_all on public.%I '
      'for all to radar_runtime using (true) with check (true)',
      table_name
    );
  end loop;
end
$$;
