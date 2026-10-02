"""Build a schema-only test fixture from a protected catalog snapshot; no production rows."""
import json
import sys

snapshot = json.load(open(sys.argv[1]))
quote = lambda name: '"' + name.replace('"', '""') + '"'
print('CREATE SCHEMA extensions; CREATE EXTENSION pgcrypto WITH SCHEMA extensions;')
print('CREATE EXTENSION "uuid-ossp" WITH SCHEMA extensions;')
print('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;')
print('SET check_function_bodies=off;')
for table in snapshot['tables']:
    print(f"CREATE TABLE public.{quote(table['name'])} ({table['columns']});")
for function in snapshot['functions']:
    print(function + ';')
for constraint in sorted(snapshot['constraints'], key=lambda c: c['type'] == 'f'):
    print(f"ALTER TABLE public.{quote(constraint['table'])} ADD CONSTRAINT {quote(constraint['name'])} {constraint['definition']};")
for view in sorted(snapshot['views'], key=lambda v: v['name'] == 'tm_project_finance_v14'):
    print(f"CREATE VIEW public.{quote(view['name'])} AS {view['definition']}")
for index in snapshot['indexes']:
    print(index.replace('INDEX ', 'INDEX IF NOT EXISTS ') + ';')
for trigger in snapshot['triggers']:
    print(trigger + ';')
