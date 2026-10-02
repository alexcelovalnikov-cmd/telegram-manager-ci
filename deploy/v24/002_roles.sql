-- Run as the schema owner, on the selected database. NO passwords in this file.
BEGIN;
DO $$BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='tm_v24_api') THEN CREATE ROLE tm_v24_api NOLOGIN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='tm_v24_calendar') THEN CREATE ROLE tm_v24_calendar NOLOGIN; END IF;
END$$;
GRANT USAGE ON SCHEMA public,tm_v24,tm_calendar TO tm_v24_api;
GRANT USAGE ON SCHEMA tm_calendar TO tm_v24_calendar;
GRANT SELECT ON public.telegram_messages,public.telegram_attachments,public.telegram_chats,public.telegram_users,
 public.telegram_chat_groups,public.telegram_chat_group_members,public.telegram_chat_group_targets,
 public.tasks,public.tm_project_links,public.integration_health,public.tm_project_import_state,
 public.tm_salary_periods_v15,public.tm_salary_series_v15,public.tm_payment_events,public.tm_payment_allocations,
 public.tm_review_focus_state_v18,public.tm_review_items TO tm_v24_api;
GRANT INSERT,UPDATE ON public.tasks TO tm_v24_api;
GRANT INSERT ON public.tm_project_links,public.tm_project_events TO tm_v24_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tm_v24_api;
-- Existing invoker-security payment functions use their canonical ledger tables.
GRANT INSERT ON public.tm_payment_events,public.tm_payment_allocations TO tm_v24_api;
GRANT SELECT ON public.tm_workstreams TO tm_v24_api;
GRANT SELECT,INSERT ON tm_v24.operations,tm_v24.identities,tm_v24.audit,tm_v24.payment_corrections TO tm_v24_api;
GRANT SELECT,INSERT,UPDATE ON tm_v24.previews,tm_v24.review_items,tm_v24.review_focus TO tm_v24_api;
GRANT SELECT ON tm_v24.migrations TO tm_v24_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA tm_v24 TO tm_v24_api;
GRANT SELECT(username,instance_id,active,revision) ON tm_calendar.users TO tm_v24_api;
GRANT SELECT ON tm_calendar.users TO tm_v24_calendar;
GRANT SELECT,INSERT,UPDATE ON tm_calendar.calendars,tm_calendar.members,tm_calendar.events TO tm_v24_api,tm_v24_calendar;
GRANT SELECT,INSERT ON tm_calendar.changes,tm_calendar.audit TO tm_v24_api,tm_v24_calendar;
GRANT SELECT ON tm_calendar.event_model TO tm_v24_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA tm_calendar TO tm_v24_api,tm_v24_calendar;
-- Exact signatures vary across the preserved V22 installations. Select by known name.
DO $$DECLARE r record;BEGIN
 FOR r IN SELECT p.oid::regprocedure signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname IN (
 'tm_group_chat_allowed_v14','tm_chat_allowed_v10','tm_content_token_v11','tm_payment_title_v18',
 'tm_project_title_v15','tm_project_title_v11','tm_project_display_title_v15','tm_evidence_valid_v14',
 'tm_record_payment_v14') LOOP
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO tm_v24_api',r.signature);
 END LOOP;
END$$;
-- Required by the preserved invoker-security task audit/topic triggers and row locks.
GRANT SELECT,INSERT ON public.tm_audit_events TO tm_v24_api;
GRANT SELECT,UPDATE ON public.meeting_topics TO tm_v24_api;
GRANT UPDATE(updated_at) ON public.telegram_chat_groups,public.telegram_attachments TO tm_v24_api;
GRANT UPDATE(content_updated_at) ON public.telegram_messages TO tm_v24_api;
GRANT UPDATE(state) ON public.tm_payment_events TO tm_v24_api;
-- The append-only V22 trigger still rejects actual UPDATE of the ledger;
-- the column grant permits SELECT FOR SHARE during correction validation.

COMMIT;
