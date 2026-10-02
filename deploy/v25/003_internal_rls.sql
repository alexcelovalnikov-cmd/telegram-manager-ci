-- V26: make inherited V22 RLS compatible with the restricted internal V24/V25 roles.
-- SQL GRANTs still define which statements/columns are writable. These policies do not grant privileges.
BEGIN;
DO $$
DECLARE t text;
BEGIN
  IF current_database() IS NULL THEN RAISE EXCEPTION 'database_required'; END IF;
  FOREACH t IN ARRAY ARRAY[
    'telegram_messages','telegram_attachments','telegram_chats','telegram_users',
    'telegram_chat_groups','telegram_chat_group_members','telegram_chat_group_targets',
    'tasks','tm_project_links','integration_health','tm_project_import_state',
    'tm_salary_periods_v15','tm_salary_series_v15','tm_payment_events','tm_payment_allocations',
    'tm_review_focus_state_v18','tm_review_items','tm_workstreams','tm_project_events',
    'tm_audit_events','meeting_topics','tm_catalog_snapshots'
  ] LOOP
    IF to_regclass('public.'||t) IS NOT NULL THEN
      IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=t AND policyname='tm_v24_api_internal'
      ) THEN
        EXECUTE format(
          'CREATE POLICY tm_v24_api_internal ON public.%I FOR ALL TO tm_v24_api USING (true) WITH CHECK (true)', t
        );
      END IF;
    END IF;
  END LOOP;

  FOREACH t IN ARRAY ARRAY['tasks','tm_audit_events','meeting_topics'] LOOP
    IF to_regclass('public.'||t) IS NOT NULL THEN
      IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=t AND policyname='tm_v24_calendar_internal'
      ) THEN
        EXECUTE format(
          'CREATE POLICY tm_v24_calendar_internal ON public.%I FOR ALL TO tm_v24_calendar USING (true) WITH CHECK (true)', t
        );
      END IF;
    END IF;
  END LOOP;
END$$;
COMMIT;
