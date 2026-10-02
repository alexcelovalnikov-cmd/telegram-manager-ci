-- Stop API writes first. Preserve operation journal and all completed business actions.
REVOKE EXECUTE ON FUNCTION public.tm_api_execute_v19(text,text,text,jsonb) FROM service_role;
NOTIFY pgrst,'reload schema';
