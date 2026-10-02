"""Empty hosted synthetic database only; never imports a catalog or customer rows.

V24 migration is real. Empty public FK/legacy-review prerequisites and V25
component columns cover these Calendar/preview tests, not full V22/V25 compatibility.
"""
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict


def main():
    dsn = os.environ.get('TM_V24_TEST_DSN', '')
    info = conninfo_to_dict(dsn)
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('TM_TEST_ALLOW_DATABASE_WRITES') != 'YES'
            or info.get('host') != '127.0.0.1' or info.get('dbname') != 'tm_v24_test'
            or info.get('user') != 'postgres' or info.get('port', '5432') != '5432'):
        raise RuntimeError('hosted_loopback_disposable_database_required')
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            name, count = conn.execute("SELECT current_database(), count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','f')").fetchone()
            if name != 'tm_v24_test' or count:
                raise RuntimeError('empty_disposable_database_required')
            conn.execute('CREATE TABLE public.tm_payment_events(id uuid PRIMARY KEY)')
            conn.execute('CREATE TABLE public.tm_review_focus_state_v18(instance_id text,scope_key text,review_id uuid)')
            conn.execute('CREATE TABLE public.tm_review_items(id uuid PRIMARY KEY,status text)')
        migration = Path(__file__).resolve().parents[2] / 'deploy/v24/001_schema.sql'
        conn.execute(migration.read_text())
        with conn.transaction():
            conn.execute("ALTER TABLE tm_calendar.calendars ADD COLUMN component_type text NOT NULL DEFAULT 'VEVENT'")
            conn.execute("ALTER TABLE tm_calendar.events ADD COLUMN component_type text NOT NULL DEFAULT 'VEVENT'")
            # Fixed synthetic credentials belong only to the ephemeral hosted service.
            conn.execute("CREATE ROLE tm_v24_api_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'synthetic-api-only'")
            conn.execute("CREATE ROLE tm_v24_calendar_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'synthetic-calendar-only'")
            conn.execute('GRANT USAGE ON SCHEMA tm_v24,tm_calendar TO tm_v24_api_login')
            conn.execute('GRANT USAGE ON SCHEMA public TO tm_v24_api_login')
            conn.execute('GRANT SELECT ON public.tm_review_focus_state_v18,public.tm_review_items TO tm_v24_api_login')
            conn.execute('GRANT SELECT,INSERT ON tm_v24.operations,tm_v24.identities,tm_v24.audit TO tm_v24_api_login')
            conn.execute('GRANT SELECT,INSERT,UPDATE ON tm_v24.previews,tm_v24.review_items,tm_v24.review_focus TO tm_v24_api_login')
            conn.execute('GRANT SELECT(username,instance_id,active,revision) ON tm_calendar.users TO tm_v24_api_login')
            conn.execute('GRANT USAGE ON SCHEMA tm_calendar TO tm_v24_calendar_login')
            conn.execute('GRANT SELECT ON tm_calendar.users TO tm_v24_calendar_login')
            for role in ('tm_v24_api_login', 'tm_v24_calendar_login'):
                conn.execute('GRANT SELECT,INSERT,UPDATE ON tm_calendar.calendars,tm_calendar.members,tm_calendar.events TO ' + role)
                conn.execute('GRANT SELECT,INSERT ON tm_calendar.changes,tm_calendar.audit TO ' + role)
                conn.execute('GRANT USAGE ON ALL SEQUENCES IN SCHEMA tm_calendar TO ' + role)
            conn.execute('GRANT USAGE ON ALL SEQUENCES IN SCHEMA tm_v24 TO tm_v24_api_login')
    print('SYNTHETIC_POSTGRES_READY: empty hosted database, real V24 migration, nonprivileged API/Calendar roles')


if __name__ == '__main__':
    main()
