-- Remove the temporary extension-schema grant from the first hosted rollout
-- and deterministically re-apply the approved runtime boundary. Fresh
-- databases already receive the same final state from 202608180001.

do $$
begin
  if not exists (
    select 1
      from pg_roles
     where rolname = 'radar_runtime'
       and rolcanlogin
       and not rolsuper
       and not rolcreatedb
       and not rolcreaterole
       and not rolreplication
       and not rolinherit
       and not rolbypassrls
       and rolconnlimit = 8
  ) then
    raise exception 'radar_runtime role attributes do not match the approved boundary';
  end if;
end
$$;

alter role radar_runtime reset all;
alter role radar_runtime set search_path = pg_catalog, public, pg_temp;
alter role radar_runtime set row_security = on;
alter role radar_runtime set statement_timeout = '20min';
alter role radar_runtime set idle_in_transaction_session_timeout = '2min';

revoke all privileges on schema extensions from radar_runtime;
revoke all privileges on all tables in schema public from radar_runtime;
revoke all privileges on all sequences in schema public from radar_runtime;
revoke all privileges on all functions in schema public from radar_runtime;
grant usage on schema public to radar_runtime;

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
