"""Run only against the isolated synthetic postgres container; never production."""
import concurrent.futures
import json
import subprocess

SSH = ['ssh', '-F', '/home/demo/Documents/GPT/Rent_Rabbit_Plugin/server-access/ssh_config', 'rr-server']
CMD = 'docker exec -i telegram-manager-db-1 psql -U postgres -d tm_synthetic -v ON_ERROR_STOP=1 -qAt'
def sql(query):
    result = subprocess.run(SSH + [CMD], input=query, text=True, capture_output=True, check=True)
    return result.stdout.strip()

sql("""
INSERT INTO public.telegram_chat_groups(id,group_key,name,reminder_list_instance_id,rules_profile)
OVERRIDING SYSTEM VALUE VALUES(990010,'concurrency-test','Synthetic concurrency','macbook-owner','projects_payments');
INSERT INTO public.integration_health(service_name,instance_id,status,details)
OVERRIDING SYSTEM VALUE VALUES('apple-sync','macbook-owner','running','{"version":"V18"}');
INSERT INTO public.tasks(id,title,description,record_kind,context_group_id,project_data)
OVERRIDING SYSTEM VALUE VALUES(990010,'Synthetic 40к','Manual note','project',990010,'{"amount_rub":40000}');
""")
ts = sql('SELECT updated_at FROM public.tasks WHERE id=990010;')
payload = {'project_id': 990010, 'expected_updated_at': ts, 'window': {'kind':'relative','within_days':7},
           'confirmed': True, 'evidence':[{'kind':'user','confirmation_ref':'concurrency-test','statement':'Confirmed seven days'}]}
# JSON is data inside a properly SQL-quoted literal, passed via stdin, never a shell command.
literal = json.dumps(payload).replace("'", "''")
query = "SELECT public.tm_api_execute_v19('macbook-owner','concurrent-key-0001','set_payment_window','" + literal + "'::jsonb);"
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(sql, [query,query]))
assert results[0] == results[1]
assert sql("SELECT count(*) FROM public.tm_project_events WHERE task_id=990010 AND action='payment_window_v18';") == '1'
# Database restart is verified separately before production cutover.
for _ in range(20):
    ready = subprocess.run(SSH + ['docker exec telegram-manager-db-1 pg_isready -U postgres'], capture_output=True)
    if ready.returncode == 0:
        break
else:
    raise RuntimeError('Test database did not restart')
assert sql(query) == results[0]
assert sql("SELECT count(*) FROM tm_api_private.operations WHERE request_key='concurrent-key-0001';") == '1'
print('Concurrent retry and persisted journal replay passed')
