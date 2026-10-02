-- Keep existing V24 role names for credential compatibility. No passwords here.
BEGIN;
GRANT USAGE ON SCHEMA public TO tm_v24_calendar;
GRANT USAGE ON SCHEMA tm_config TO tm_v24_api,tm_v24_calendar;
GRANT SELECT ON ALL TABLES IN SCHEMA tm_config TO tm_v24_api;
GRANT INSERT,UPDATE ON tm_config.workspaces,tm_config.documents,tm_config.entities,tm_config.payment_expectations,tm_config.reminder_bindings TO tm_v24_api;
GRANT INSERT ON tm_config.history,tm_config.analysis_receipts,tm_config.payment_details TO tm_v24_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA tm_config TO tm_v24_api,tm_v24_calendar;
GRANT SELECT,INSERT,UPDATE ON public.telegram_chat_groups,public.telegram_chat_group_members,public.telegram_chat_group_targets TO tm_v24_api;
GRANT SELECT ON public.tm_catalog_snapshots TO tm_v24_api;
GRANT SELECT ON tm_config.workspaces,tm_config.documents,tm_config.reminder_bindings TO tm_v24_calendar;
GRANT UPDATE(manual_title,manual_description,hidden) ON tm_config.reminder_bindings TO tm_v24_calendar;
-- The DAV adapter only updates an existing authorized binding. It cannot create
-- arbitrary tasks, payment events, project costs, rules, accounts or memberships.
GRANT SELECT ON public.tasks TO tm_v24_calendar;
GRANT UPDATE(title,description,status,due_at,reminder_due_spec,tags,completed_at,cancelled_at,updated_at,reminder_active,project_data) ON public.tasks TO tm_v24_calendar;
GRANT SELECT,INSERT ON public.tm_audit_events TO tm_v24_calendar;
GRANT SELECT,UPDATE ON public.meeting_topics TO tm_v24_calendar;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tm_v24_calendar;
DO $$DECLARE r record;BEGIN
 FOR r IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname IN ('tm_task_audit_v8','tm_project_guard_v11','rr_sync_topic_from_task_v1','rr_assign_task_group_v1','rr_target_revision_v7') LOOP
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO tm_v24_api,tm_v24_calendar',r.signature);
 END LOOP;
END$$;
COMMIT;
