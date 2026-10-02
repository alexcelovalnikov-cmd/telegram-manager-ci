\set ON_ERROR_STOP on
SET timezone='UTC';
SET datestyle='ISO,YMD';
SET search_path=pg_catalog;
SELECT format('SELECT %L AS relation, count(*) AS rows, md5(coalesce(string_agg(to_jsonb(t)::text, E''\n'' ORDER BY to_jsonb(t)::text COLLATE "C"),'''')) AS digest FROM %I.%I t;',n.nspname||'.'||c.relname,n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind='r' AND n.nspname IN ('public','tm_api_private','supabase_migrations') ORDER BY n.nspname,c.relname
\gexec
SELECT 'functions',count(*),md5(string_agg(pg_get_functiondef(p.oid), E'\n' ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)))
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('public','tm_api_private') AND p.prokind='f';
SELECT 'views',count(*),md5(string_agg(pg_get_viewdef(c.oid),E'\n' ORDER BY n.nspname,c.relname))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='v';
SELECT 'constraints',count(*),md5(string_agg(pg_get_constraintdef(c.oid),E'\n' ORDER BY c.conrelid::regclass::text,c.conname))
FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname IN ('public','tm_api_private');
