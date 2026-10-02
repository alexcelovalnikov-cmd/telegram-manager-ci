-- Additive migration. Run ONLY after the V22 schema preflight and a verified backup.
-- Neither the original schema nor the current display profile is replaced.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT pg_advisory_xact_lock(74240929);
CREATE SCHEMA IF NOT EXISTS tm_v24;
CREATE SCHEMA IF NOT EXISTS tm_calendar;
REVOKE ALL ON SCHEMA tm_v24,tm_calendar FROM PUBLIC;
CREATE TABLE IF NOT EXISTS tm_v24.migrations(version integer PRIMARY KEY,installed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_v24.previews(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),instance_id text NOT NULL,request_key text NOT NULL,
 payload_digest text NOT NULL,payload jsonb NOT NULL,plans jsonb NOT NULL,changes jsonb NOT NULL,
 preview_digest text NOT NULL,expires_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'preview' CHECK(status IN ('preview','applied','invalidated')),
 result jsonb,confirmation_ref text,applied_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(instance_id,request_key), CHECK(jsonb_typeof(payload)='array'));
CREATE TABLE IF NOT EXISTS tm_v24.operations(
 instance_id text NOT NULL,operation text NOT NULL,request_key text NOT NULL,input_digest text NOT NULL,
 result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(instance_id,operation,request_key));
CREATE TABLE IF NOT EXISTS tm_v24.identities(
 instance_id text NOT NULL,dedupe_key text NOT NULL,entity text NOT NULL,target_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(instance_id,dedupe_key));
CREATE TABLE IF NOT EXISTS tm_v24.audit(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,instance_id text NOT NULL,operation text NOT NULL,
 target_id text,before_state jsonb,after_state jsonb,evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_v24.payment_corrections(
 original_id uuid PRIMARY KEY REFERENCES public.tm_payment_events(id),
 offset_id uuid NOT NULL REFERENCES public.tm_payment_events(id),
 replacement_id uuid NOT NULL REFERENCES public.tm_payment_events(id),reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_v24.review_items(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),instance_id text NOT NULL,item_key text NOT NULL,
 payload jsonb NOT NULL,fingerprint text NOT NULL,revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','awaiting_user','snoozed','resolved','dismissed')),
 snoozed_until timestamptz,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(instance_id,item_key));
CREATE TABLE IF NOT EXISTS tm_v24.review_focus(
 instance_id text PRIMARY KEY,review_id uuid REFERENCES tm_v24.review_items(id),
 version integer NOT NULL DEFAULT 1 CHECK(version>0),updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS review_queue_v24 ON tm_v24.review_items(instance_id,status,created_at,id);
CREATE TABLE IF NOT EXISTS tm_calendar.users(
 username text PRIMARY KEY CHECK(username ~ '^[a-zA-Z0-9_-]{1,64}$'),
 instance_id text UNIQUE,password_hash text NOT NULL CHECK(password_hash LIKE '$argon2id$%'),
 active boolean NOT NULL DEFAULT true,revision integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tm_calendar.calendars(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),slug text NOT NULL UNIQUE,title text NOT NULL,
 timezone text NOT NULL DEFAULT 'Asia/Yekaterinburg',props jsonb NOT NULL DEFAULT '{"tag":"VCALENDAR"}',
 revision bigint NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),deleted_at timestamptz);
CREATE TABLE IF NOT EXISTS tm_calendar.members(
 calendar_id uuid NOT NULL REFERENCES tm_calendar.calendars(id),username text NOT NULL REFERENCES tm_calendar.users(username),
 role text NOT NULL CHECK(role IN ('owner','viewer','editor')),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','revoked')),
 revision integer NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 revoked_at timestamptz,PRIMARY KEY(calendar_id,username));
CREATE TABLE IF NOT EXISTS tm_calendar.events(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),calendar_id uuid NOT NULL REFERENCES tm_calendar.calendars(id),
 caldav_uid text NOT NULL,href text NOT NULL,fields jsonb NOT NULL,icalendar text NOT NULL,
 etag text NOT NULL,revision bigint NOT NULL DEFAULT 1,created_by text NOT NULL REFERENCES tm_calendar.users(username),
 updated_by text NOT NULL REFERENCES tm_calendar.users(username),source text NOT NULL,
 sync_state text NOT NULL DEFAULT 'consistent' CHECK(sync_state='consistent'),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),deleted_at timestamptz,
 UNIQUE(calendar_id,caldav_uid),UNIQUE(calendar_id,href),CHECK(octet_length(icalendar)<=2097152));
CREATE INDEX IF NOT EXISTS calendar_events_active ON tm_calendar.events(calendar_id,id) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS tm_calendar.changes(
 calendar_id uuid NOT NULL REFERENCES tm_calendar.calendars(id),revision bigint NOT NULL,
 href text NOT NULL,deleted boolean NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(calendar_id,revision,href));
CREATE TABLE IF NOT EXISTS tm_calendar.audit(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,actor text NOT NULL,operation text NOT NULL,target_id text NOT NULL,
 before_state jsonb,after_state jsonb,created_at timestamptz NOT NULL DEFAULT now());
CREATE OR REPLACE VIEW tm_calendar.event_model AS
 SELECT id AS internal_id,caldav_uid,calendar_id,fields->>'title' AS title,fields->>'description' AS description,
 fields->>'location' AS location,fields->>'start_at' AS start_at,fields->>'end_at' AS end_at,
 fields->>'timezone' AS timezone,(fields->>'all_day')::boolean AS all_day,fields->>'recurrence' AS recurrence,
 fields->>'status' AS status,revision,etag,created_at,updated_at,created_by,updated_by,source,sync_state,deleted_at
 FROM tm_calendar.events;
CREATE OR REPLACE FUNCTION tm_v24.reject_history_change() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $$BEGIN RAISE EXCEPTION 'append_only_history'; END$$;
DO $$DECLARE s text;t text;BEGIN
 FOR s,t IN SELECT * FROM (VALUES ('tm_v24','operations'),('tm_v24','audit'),('tm_v24','identities'),
  ('tm_v24','payment_corrections'),('tm_calendar','audit'),('tm_calendar','changes')) p(s,t) LOOP
  EXECUTE format('DROP TRIGGER IF EXISTS append_only_v24 ON %I.%I',s,t);
  EXECUTE format('CREATE TRIGGER append_only_v24 BEFORE UPDATE OR DELETE ON %I.%I FOR EACH ROW EXECUTE FUNCTION tm_v24.reject_history_change()',s,t);
 END LOOP;END$$;
-- Prevent the old focus selector from replacing an unanswered generic card.
CREATE OR REPLACE FUNCTION tm_v24.guard_legacy_focus() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 BEGIN
 IF NEW.scope_key='personal-review' AND NEW.review_id IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('tm-v24-review:'||NEW.instance_id,0));
  IF EXISTS(SELECT 1 FROM tm_v24.review_focus f JOIN tm_v24.review_items r ON r.id=f.review_id
    WHERE f.instance_id=NEW.instance_id AND r.status IN ('open','awaiting_user')) THEN
   RAISE EXCEPTION 'generic_review_question_still_active';
  END IF;
 END IF; RETURN NEW;
 END$$;
DROP TRIGGER IF EXISTS tm_v24_focus_guard ON public.tm_review_focus_state_v18;
CREATE TRIGGER tm_v24_focus_guard BEFORE INSERT OR UPDATE ON public.tm_review_focus_state_v18
 FOR EACH ROW EXECUTE FUNCTION tm_v24.guard_legacy_focus();
REVOKE ALL ON ALL TABLES IN SCHEMA tm_v24,tm_calendar FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA tm_v24 FROM PUBLIC;
INSERT INTO tm_v24.migrations(version) VALUES(24) ON CONFLICT DO NOTHING;
COMMIT;
