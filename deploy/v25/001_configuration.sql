-- ADDITIVE V25. Apply to a disposable staging database first, after V24 migrations.
-- No existing group, project, rule, display_profile or native reminder is migrated.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT pg_advisory_xact_lock(74240928);
CREATE SCHEMA IF NOT EXISTS tm_config;
REVOKE ALL ON SCHEMA tm_config FROM PUBLIC;
CREATE TABLE IF NOT EXISTS tm_config.workspaces(
 id uuid PRIMARY KEY, instance_id text NOT NULL, workspace_key text NOT NULL,
 name text NOT NULL, group_id bigint UNIQUE REFERENCES public.telegram_chat_groups(id),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
 settings jsonb NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(instance_id,workspace_key));
CREATE TABLE IF NOT EXISTS tm_config.defaults(
 key text PRIMARY KEY,body jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1);
INSERT INTO tm_config.defaults(key,body) VALUES('formatting',
 '{"received_marker":"💶","awaiting_marker":"⚡️","partial_separator":" + ","thousands_suffix":"к","decimal_separator":",","currency_suffix":" ₽","tag_separator":" ","date_format":"MM.DD"}'),
 ('templates','{"task":{"title":"{tags} {title}","description":"{description}"},"call":{"title":"{tags} {title}","description":"{description}"},"project":{"title":"{tags} {payment_marker}{date_prefix}{label}{amount_separator}{payment_display}","description":"{description}"},"shoot":{"title":"{tags} {title}","description":"{description}"},"payment":{"title":"{title}","description":"{description}"},"awaiting_payment":{"title":"{title}","description":"{description}"}}') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS tm_config.documents(
 id uuid PRIMARY KEY,instance_id text NOT NULL,workspace_id uuid REFERENCES tm_config.workspaces(id),
 document_key text NOT NULL,kind text NOT NULL CHECK(kind IN ('rule','template','entity_type')),
 body jsonb NOT NULL,status text NOT NULL CHECK(status IN ('proposed','active','disabled','deleted')),
 origin text NOT NULL CHECK(origin IN ('explicit_user','review_generalization','import')),
 review_id text,evidence jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE NULLS NOT DISTINCT(instance_id,workspace_id,document_key));
CREATE TABLE IF NOT EXISTS tm_config.history(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,instance_id text NOT NULL,
 resource_type text NOT NULL,resource_id uuid NOT NULL,revision bigint NOT NULL,
 snapshot jsonb NOT NULL,evidence jsonb NOT NULL,operation text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(resource_type,resource_id,revision));
CREATE TABLE IF NOT EXISTS tm_config.entities(
 id uuid PRIMARY KEY,instance_id text NOT NULL,workspace_id uuid NOT NULL REFERENCES tm_config.workspaces(id),
 entity_type text NOT NULL,data jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_config.analysis_receipts(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,instance_id text NOT NULL,
 workspace_id uuid REFERENCES tm_config.workspaces(id),configuration_token text NOT NULL,
 operation text NOT NULL,target_id text,rule_refs jsonb NOT NULL,reason text NOT NULL,
 evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_config.payment_details(
 payment_id uuid PRIMARY KEY REFERENCES public.tm_payment_events(id),
 work_amount numeric(14,2) NOT NULL CHECK(work_amount>0),tax_amount numeric(14,2),document_total numeric(14,2),
 CHECK(tax_amount IS NULL OR tax_amount>=0),CHECK(document_total IS NULL OR document_total>=work_amount),
 CHECK(tax_amount IS NULL OR document_total IS NULL OR document_total=work_amount+tax_amount));
CREATE TABLE IF NOT EXISTS tm_config.payment_expectations(
 id uuid PRIMARY KEY,instance_id text NOT NULL,task_id bigint NOT NULL REFERENCES public.tasks(id),
 amount numeric(14,2) NOT NULL CHECK(amount>0),expected_at text,
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','fulfilled','cancelled')),
 evidence jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_config.reminder_bindings(
 task_id bigint PRIMARY KEY REFERENCES public.tasks(id),resource_id uuid UNIQUE NOT NULL REFERENCES tm_calendar.events(id),
 workspace_id uuid NOT NULL REFERENCES tm_config.workspaces(id),
 base_title text NOT NULL,base_description text NOT NULL DEFAULT '',manual_title text,manual_description text,
 hidden boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE tm_calendar.calendars ADD COLUMN IF NOT EXISTS component_type text NOT NULL DEFAULT 'VEVENT';
ALTER TABLE tm_calendar.events ADD COLUMN IF NOT EXISTS component_type text NOT NULL DEFAULT 'VEVENT';
DO $$BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='tm_collection_component_v25' AND conrelid='tm_calendar.calendars'::regclass) THEN
  ALTER TABLE tm_calendar.calendars ADD CONSTRAINT tm_collection_component_v25 CHECK(component_type IN ('VEVENT','VTODO'));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='tm_resource_component_v25' AND conrelid='tm_calendar.events'::regclass) THEN
  ALTER TABLE tm_calendar.events ADD CONSTRAINT tm_resource_component_v25 CHECK(component_type IN ('VEVENT','VTODO'));
 END IF;
END$$;
CREATE OR REPLACE VIEW tm_config.reminders AS
 SELECT id,calendar_id AS list_id,caldav_uid,fields,revision,etag,sync_state,created_at,updated_at,deleted_at
 FROM tm_calendar.events WHERE component_type='VTODO';
-- Read/write services have no authority to rewrite audit, rule or payment history.
DO $$DECLARE t text;BEGIN
 FOREACH t IN ARRAY ARRAY['history','analysis_receipts','payment_details'] LOOP
  EXECUTE format('DROP TRIGGER IF EXISTS append_only_v25 ON tm_config.%I',t);
  EXECUTE format('CREATE TRIGGER append_only_v25 BEFORE UPDATE OR DELETE ON tm_config.%I FOR EACH ROW EXECUTE FUNCTION tm_v24.reject_history_change()',t);
 END LOOP;
END$$;
-- Native Apple writers must never take over a server-managed task. All V25
-- writes set a transaction-local actor after authenticated instance/ACL checks.
CREATE OR REPLACE FUNCTION tm_config.protect_server_task() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE owner_instance text; destination text;
 BEGIN
  SELECT w.instance_id,w.settings->'reminder_list'->>'mode' INTO owner_instance,destination
    FROM tm_config.workspaces w WHERE w.group_id=COALESCE(NEW.context_group_id,OLD.context_group_id);
  IF destination IN ('server_new','server_existing') AND
     current_setting('tm.v25_actor',true) IS DISTINCT FROM owner_instance THEN
    RAISE EXCEPTION 'server_managed_entity_use_v25_api';
  END IF;
  RETURN NEW;
 END$$;
DROP TRIGGER IF EXISTS tm_v25_server_task_guard ON public.tasks;
CREATE TRIGGER tm_v25_server_task_guard BEFORE INSERT OR UPDATE ON public.tasks
 FOR EACH ROW EXECUTE FUNCTION tm_config.protect_server_task();
REVOKE ALL ON ALL TABLES IN SCHEMA tm_config FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA tm_config FROM PUBLIC;
INSERT INTO tm_v24.migrations(version) VALUES(25) ON CONFLICT DO NOTHING;
COMMIT;
