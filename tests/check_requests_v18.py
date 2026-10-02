"""Synthetic isolated-Postgres integration with the unchanged V18 Mac parser."""
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, '/home/demo/Downloads/Telegram_Manager_V18')
from tm_project_sync import ProjectSync
from tm_projects import desired_state, merge_states
from tm_payment_window import personal_task

SSH = ['ssh', '-F', '/home/demo/Documents/GPT/Rent_Rabbit_Plugin/server-access/ssh_config', 'rr-server']
CMD = 'docker exec -i telegram-manager-db-1 psql -U postgres -d tm_request_test -v ON_ERROR_STOP=1 -qAt'
def sql(query, fails=None):
    r = subprocess.run(SSH + [CMD], input=query, text=True, capture_output=True)
    if fails:
        assert r.returncode and fails in r.stderr, r.stderr
        return
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
def lit(value):
    if value is None:
        return 'NULL'
    if isinstance(value, (dict, list)):
        return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(value).replace("'", "''") + "'"
def task(i=990100):
    return json.loads(sql(f'SELECT to_jsonb(t) FROM tasks t WHERE id={i};'))
def write(key, operation, payload, fails=None):
    raw = sql('SELECT tm_api_execute_v19('+','.join(map(lit, ['macbook-owner', key, operation, payload]))+');', fails)
    return json.loads(raw) if raw else None
EVIDENCE = [{'kind':'user','confirmation_ref':'synthetic-phase4','statement':'Explicit synthetic request instruction'}]
def transition(key, state, i=990100, fails=None):
    p={'project_id':i,'expected_updated_at':task(i)['updated_at'],'state':state,'confirmed':True,'evidence':EVIDENCE}
    return write(key,'set_project_request_state',p,fails),p
class Capture(ProjectSync):
    def rpc(self, name, p):
        return json.loads(sql('SELECT '+name+'('+','.join(k+'=>'+lit(v) for k,v in p.items())+');'))

def ack(title=None, completed=False, i=990100, fails=None):
    t=personal_task(task(i))
    state={'title':title or t['title'],'completed':completed,'notes':'','due_spec':{'kind':'none'},'url':'rentrabbit://task/'+str(i),'fingerprint':'synthetic-'+t['updated_at']}
    try:
        Capture(None,None).ack(t, {'reminder_list_id':'synthetic-list'}, {'id':'synthetic-reminder-'+str(i)},state)
        assert not fails, 'Expected failure: '+str(fails)
    except AssertionError as e:
        if not fails or fails not in str(e): raise

sql("""
INSERT INTO telegram_chat_groups(id,group_key,name,reminder_list_instance_id,reminder_list_id,rules_profile,import_existing_reminders)
OVERRIDING SYSTEM VALUE VALUES(990100,'phase4','Synthetic requests','macbook-owner','synthetic-list','projects_payments',true);
INSERT INTO integration_health(service_name,instance_id,status,details) VALUES('apple-sync','macbook-owner','running','{"version":"V18"}')
ON CONFLICT(service_name,instance_id) DO UPDATE SET last_heartbeat_at=now(),status='running';
INSERT INTO tasks(id,title,record_kind,context_group_id,project_data,apple_reminder_id,apple_reminder_list_id,project_sync_snapshot)
OVERRIDING SYSTEM VALUE VALUES(990100,'10.20 Synthetic - 40к','project',990100,'{"label":"Synthetic","date_mmdd":"10.20","date_iso":"2026-10-20","amount_rub":"40000.00","work_status":"planned","display_protocol":"v18_payment_window"}',
'synthetic-reminder-990100','synthetic-list','{"title":"10.20 Synthetic - 40к","notes":"","completed":false}'),
(990101,'No date - 5к','project',990100,'{"label":"No date","date_mmdd":null,"date_iso":null,"amount_rub":"5000.00","work_status":"planned","display_protocol":"v18_payment_window"}',
'synthetic-reminder-990101','synthetic-list','{"title":"No date - 5к","notes":"","completed":false}');
""")
original=task()
result,p=transition('phase4-pending-0001','pending')
assert task()['title']=='➕ 10.20 Synthetic - 40к'
assert task()['project_key']==original['project_key']
# A unchanged native snapshot receives only the new title under V18's 3-way merge.
t=personal_task(task()); observed=dict(t['project_sync_snapshot'])
merged=merge_states(t,observed)
assert merged['title']==t['title'] and not merged['completed']
ack()
t=task(); assert t['project_data']['label']=='Synthetic' and t['project_data']['date_iso']=='2026-10-20'
assert t['project_data']['amount_rub']=='40000.00'
# A native amount/label edit is preserved without corrupting date or lifecycle.
ack('➕ 10.20 Synthetic edited - 41к')
t=task(); assert t['project_data']['label']=='Synthetic edited' and t['project_data']['amount_rub']=='41000.00'
ack('10.20 Synthetic edited - 41к',fails='request_marker_requires_review')
ack(completed=True,fails='unconfirmed_request_cannot_complete')
# Receipt/forecast remain independent of request state, and confirmation keeps identity.
window={'project_id':990100,'expected_updated_at':task()['updated_at'],'window':{'kind':'relative','within_days':7},'confirmed':True,'evidence':EVIDENCE}
write('phase4-window-0001','set_payment_window',window)
assert task()['title'].startswith('➕ ') and '⚡' in task()['title']
confirm,pconfirm=transition('phase4-confirm-0001','confirmed')
t=task(); assert not t['title'].startswith('➕') and '⚡' in t['title'] and t['payment_status']=='awaiting'
assert t['id']==original['id'] and t['project_key']==original['project_key'] and t['apple_reminder_id']==original['apple_reminder_id']
assert write('phase4-confirm-0001','set_project_request_state',pconfirm)==confirm
ack()
transition('phase4-backwards-01','pending',fails='request_transition_requires_review')
transition('phase4-pending-0002','pending',i=990101)
ack(i=990101)
assert task(990101)['project_data']['date_mmdd'] is None
transition('phase4-reject-00001','rejected',i=990101)
ack(i=990101)
t=task(990101); assert t['status']=='waiting' and t['completed_at'] is None and t['cancelled_at'] is not None
assert t['title']=='Отклонено: No date - 5к' and t['project_data']['label']=='No date'
snapshot=json.loads(sql("SELECT tm_financial_snapshot_v19('macbook-owner');"))
assert len(snapshot['items'])==2 and next(p for p in snapshot['items'] if p['id']==990101)['project_data']['request_state']=='rejected'
# A real proposal follows existing duplicate guards; repeat never creates a second row.
sql("""
INSERT INTO telegram_chats(chat_id,chat_name) VALUES(-990100,'Synthetic');
INSERT INTO telegram_chat_group_members(group_id,chat_id) VALUES(990100,-990100);
INSERT INTO telegram_messages(chat_id,message_id,text) VALUES(-990100,1,'Prospective new project');
INSERT INTO tm_project_import_state(group_id,instance_id,list_id,status,last_snapshot_at,reminder_count) VALUES(990100,'macbook-owner','synthetic-list','complete',now(),2);
INSERT INTO tm_project_proposals(request_key,group_id,chat_id,kind,patch,evidence,confidence)
OVERRIDING SYSTEM VALUE VALUES('phase4-new-proposal',990100,-990100,'create','{"label":"New prospective","date_mmdd":"11.01","amount_rub":10000}',
jsonb_build_array(jsonb_build_object('kind','telegram','chat_id',-990100,'message_id',1,'content_token',tm_content_token_v11(-990100,1))),1);
""")
pr=json.loads(sql("SELECT to_jsonb(p) FROM tm_project_proposals p WHERE request_key='phase4-new-proposal';"))
p={'proposal_id':pr['id'],'expected_proposal_updated_at':pr['updated_at'],'confirmed':True,'evidence':EVIDENCE}
r=write('phase4-create-00001','create_project_request',p)
assert task(r['task_id'])['title']=='➕ 11.01 New prospective - 10к'
assert write('phase4-create-00001','create_project_request',p)==r
assert sql('SELECT count(*) FROM tasks;')=='3'
assert sql('SELECT count(*) FROM tm_project_links WHERE task_id='+str(r['task_id'])+';')=='1'
print('V18 sync, native edits, pending/confirm/reject, payment independence, preserved identity, create and retry passed')
