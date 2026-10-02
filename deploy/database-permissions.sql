REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public, extensions, tm_api_private TO service_role;
REVOKE ALL ON ALL TABLES IN SCHEMA public, tm_api_private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public, tm_api_private FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public, tm_api_private FROM PUBLIC, anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
GRANT SELECT, INSERT ON tm_api_private.operations TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA tm_api_private TO service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA tm_api_private REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
NOTIFY pgrst, 'reload schema';

DO $$ BEGIN IF to_regclass('tm_api_private.review_proposals_v22') IS NOT NULL THEN
 GRANT SELECT, INSERT ON tm_api_private.review_proposals_v22 TO service_role;
 END IF; END $$;
