"""REAL database/CalDAV tests. No mocks replace persistence or protocol code.

Run only against a freshly restored, disposable tm_v24_test with V24+V25
migrations. Synthetic fixtures are append-only and scoped to random instances.
"""
import os
import uuid
from types import SimpleNamespace
import pytest
pytest.importorskip('psycopg',reason='Real PostgreSQL driver not installed')
pytest.importorskip('icalendar',reason='Real iCalendar dependency not installed')
pytest.importorskip('radicale',reason='Real CalDAV implementation not installed')
from argon2 import PasswordHasher
from tm_api.v24.database import Database
from tm_api.v24.engine import Engine
from tm_api.v24.common import Rejected
from tm_api.v25.reading import ConfigReader
from tm_calendar import repository as calendar

EVIDENCE=[{'kind':'user','statement':'Разрешаю это синтетическое тестовое изменение.','confirmation_ref':'synthetic-v25-user-request'}]

def key(prefix='test'):
    return prefix+'-'+uuid.uuid4().hex

def execute(e,operation,changes=None,**kwargs):
    request=key();m={'operation':operation,'changes':changes or {},'evidence':EVIDENCE,**kwargs}
    if operation.startswith('create_'):m['dedupe_key']=key('candidate')
    p=e.preview(request,[m])
    return e.apply(request,p['preview_id'],p['preview_digest'],True,'synthetic-user-confirmation')['results'][0]

@pytest.fixture
def system():
    dsn=os.environ.get('TM_V24_TEST_DSN')
    if not dsn or os.environ.get('TM_TEST_ALLOW_DATABASE_WRITES')!='YES':pytest.skip('Disposable database opt-in required')
    admin=Database(dsn)
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT current_database() AS name')['name']=='tm_v24_test','Non-test database refused'
        assert tx.one('SELECT version FROM tm_v24.migrations WHERE version=25'), 'Apply V25 migrations first'
    instance=key('instance');owner='owner_'+uuid.uuid4().hex[:12];viewer='viewer_'+uuid.uuid4().hex[:12];password=key('synthetic-secret')
    chats=[int(uuid.uuid4().hex[:13],16)+i for i in range(5)]
    with admin.transaction() as tx:
        calendar.lock(tx)
        for name,inst in [(owner,instance),(viewer,None)]:
            tx.execute('INSERT INTO tm_calendar.users(username,instance_id,password_hash) VALUES(%s,%s,%s)',(name,inst,PasswordHasher().hash(password)))
        for i,cid in enumerate(chats):tx.execute('INSERT INTO public.telegram_chats(chat_id,chat_name,enabled) VALUES(%s,%s,true)',(cid,'Synthetic chat '+str(i)))
    db=Database(os.environ.get('TM_V24_TEST_API_DSN') or dsn)
    e=Engine(db,instance,configurable=True);reader=ConfigReader(db,instance)
    return SimpleNamespace(admin=admin,db=db,e=e,reader=reader,instance=instance,owner=owner,viewer=viewer,password=password,chats=chats)

def workspace(s):
    return execute(s.e,'create_workspace',{
        'key':'post-'+uuid.uuid4().hex[:12],'name':'Постпродакшен','chat_ids':s.chats,
        'reminder_list':{'mode':'server_new','title':'Постпродакшен'},'default_tags':['пост'],
        'analysis':{'tracked_kinds':['source_files','edit_version','client_feedback','approval','deadline']},
        'documents':[{'key':'deadline','kind':'rule','body':{'text':'Предложенная дата не является подтвержденным дедлайном.','decision_key':'deadline.proposal','value':'tentative'}}]
    })['id']

def ctx(s,w):
    value=s.reader.read('analysis_context',workspace_id=w)
    return {'workspace_id':w,'configuration_token':value['configuration_token'],'analysis':{'reason':'synthetic test','rule_refs':[{'id':r['id'],'revision':r['revision']} for r in value['active_rules']]}}

def taskrow(s,ident):
    with s.admin.transaction(read_only=True) as tx:return tx.one('SELECT * FROM public.tasks WHERE id=%s',(ident,))

def test_workspace_five_chats_server_task_and_repeat(system):
    s=system;w=workspace(s);c=ctx(s,w)
    v=s.reader.read('analysis_context',workspace_id=w)
    assert sorted(x['chat_id'] for x in v['sources'])==sorted(s.chats)
    assert v['workspace']['settings']['reminder_list']['mode']=='server_new'
    request=key();mutation={'operation':'create_task','dedupe_key':key('candidate'),'changes':{'title':'Проверить материалы'},'evidence':EVIDENCE,**c}
    preview=s.e.preview(request,[mutation]);result=s.e.apply(request,preview['preview_id'],preview['preview_digest'],True,'synthetic-user-confirmation')
    assert s.e.apply(request,preview['preview_id'],preview['preview_digest'],True,'synthetic-user-confirmation')==result
    t=taskrow(s,result['results'][0]['id']);assert t['title']=='#пост Проверить материалы' and t['target_app'] is None and not t['reminder_active']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id FROM tm_config.reminder_bindings WHERE task_id=%s',(t['id'],))
        todo=calendar.event(tx,s.owner,b['resource_id']);assert todo['component_type']=='VTODO' and todo['fields']['title']==t['title']
    with pytest.raises(Rejected,match='duplicate'):
        execute(s.e,'create_task',{'title':'Проверить материалы'},**ctx(s,w))

def test_learning_proposed_activation_and_stale_analysis(system):
    s=system;w=workspace(s);old=ctx(s,w)
    r=execute(s.e,'create_rule',{'key':'feedback','origin':'review_generalization','status':'active','review_id':'synthetic-review','body':{'text':'Правки клиента оформлять отдельными задачами.'}},workspace_id=w)
    assert r['status']=='proposed'
    with pytest.raises(Rejected,match='analysis_context'):
        execute(s.e,'create_task',{'title':'Старый анализ'},**old)
    active=execute(s.e,'activate_rule',target_id=r['id'],expected_revision=r['revision'],workspace_id=w)
    assert active['status']=='active'
    versions=s.reader.read('history',resource_id=r['id'])['items']
    assert [x['revision'] for x in versions]==[1,2]
    disabled=execute(s.e,'disable_rule',target_id=r['id'],expected_revision=active['revision'],workspace_id=w)
    restored=execute(s.e,'restore_rule_version',{'version':2},target_id=r['id'],expected_revision=disabled['revision'],workspace_id=w)
    assert restored['revision']==4 and restored['status']=='active'

def test_net_cost_two_partial_receipts_and_correction(system):
    s=system;w=workspace(s)
    project=execute(s.e,'create_project',{'label':'Синтетический ВК бэк','amount_rub':'40000'},**ctx(s,w))['id']
    def receipt(value):
        t=taskrow(s,project)
        return execute(s.e,'create_payment',{'kind':'received','occurred_at':'2026-09-28T12:00:00+05:00','amounts':{'work_amount':value},'allocations':[{'task_id':project,'amount':value,'expected_revision':t['updated_at']}]},**ctx(s,w))
    receipt('20000');t=taskrow(s,project)
    assert t['payment_status']=='partial' and '💶20к + 20к' in t['title']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id,hidden FROM tm_config.reminder_bindings WHERE task_id=%s',(project,))
        todo=tx.one('SELECT deleted_at,fields FROM tm_calendar.events WHERE id=%s::uuid',(b['resource_id'],))
        assert b['hidden'] is False and todo['deleted_at'] is None
        assert '💶20к + 20к' in todo['fields']['title']

    second=receipt('20000');t=taskrow(s,project)
    assert t['payment_status']=='paid' and '💶' not in t['title'] and not t['project_archived_at']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id,hidden FROM tm_config.reminder_bindings WHERE task_id=%s',(project,))
        todo=tx.one('SELECT deleted_at FROM tm_calendar.events WHERE id=%s::uuid',(b['resource_id'],))
        assert b['hidden'] is True and todo['deleted_at'] is not None
        assert (t['payment_evidence'] or {}).get('reminder_projection')=='hidden_paid'

    with s.admin.transaction(read_only=True) as tx:entry=tx.one('SELECT created_at FROM public.tm_payment_events WHERE id=%s::uuid',(second['id'],))
    correction=execute(s.e,'update_payment',{'amount':'10000','allocations':[{'task_id':project,'amount':'10000'}],'reason':'Synthetic receipt corrected'},target_id=second['id'],expected_revision=entry['created_at'],**ctx(s,w))
    assert correction['history_preserved']
    fin=s.reader.read('project_balance',resource_id=str(project))['balance']
    assert fin['received_net']=='30000.00' or fin['received_net']=='30000'
    t=taskrow(s,project)
    assert t['payment_status']=='partial' and '💶30к + 10к' in t['title']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id,hidden FROM tm_config.reminder_bindings WHERE task_id=%s',(project,))
        todo=tx.one('SELECT deleted_at,fields FROM tm_calendar.events WHERE id=%s::uuid',(b['resource_id'],))
        assert b['hidden'] is False and todo['deleted_at'] is None
        assert '💶30к + 10к' in todo['fields']['title']

    execute(s.e,'set_project_cost',{'work_amount':'30000','tax_amount':'1800','document_total':'31800'},target_id=str(project),expected_revision=t['updated_at'],**ctx(s,w))
    t=taskrow(s,project);assert t['project_data']['amount_rub']=='30000' and t['payment_status']=='paid'
    assert '💶' not in t['title'] and '31800' not in t['title']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id,hidden FROM tm_config.reminder_bindings WHERE task_id=%s',(project,))
        todo=tx.one('SELECT deleted_at FROM tm_calendar.events WHERE id=%s::uuid',(b['resource_id'],))
        assert b['hidden'] is True and todo['deleted_at'] is not None

def test_legacy_record_payment_uses_v25_ledger_and_same_key_is_stable(system):
    s=system;w=workspace(s)
    project=execute(s.e,'create_project',{'label':'Synthetic legacy receipt','amount_rub':'40000'},**ctx(s,w))['id']
    t=taskrow(s,project)
    with s.admin.transaction(read_only=True) as tx:
        group=tx.one('SELECT group_key FROM public.telegram_chat_groups WHERE id=%s',(t['context_group_id'],))
    request=key('legacy-record-payment')
    payload={
        'group_key':group['group_key'],
        'event':{'kind':'received','amount':'40000','currency':'RUB','method':'bank',
                 'counterparty':'Synthetic payer','external_ref':'synthetic-receipt',
                 'occurred_at':'2026-10-01T12:00:00+05:00','state':'confirmed'},
        'allocations':[{'task_id':project,'expected_updated_at':t['updated_at'],
                        'work_key':'main','amount':'40000'}],
        'evidence':EVIDENCE,'confirmed':True,
    }
    first=s.e.record_payment_compat(request,payload)
    assert first['compatibility']=='v25_direct_ledger' and first['applied'] is True
    assert s.e.record_payment_compat(request,payload)==first
    with s.admin.transaction(read_only=True) as tx:
        count=tx.one('SELECT count(*) AS n FROM public.tm_payment_events WHERE id=%s::uuid',(first['id'],))
    assert count['n']==1 and taskrow(s,project)['payment_status']=='paid'


def test_payment_promise_is_not_receipt(system):
    s=system;w=workspace(s);p=execute(s.e,'create_project',{'label':'Synthetic promise','amount_rub':'20000'},**ctx(s,w))['id'];t=taskrow(s,p)
    execute(s.e,'create_payment_expectation',{'task_id':p,'amount':'20000','expected_at':'2026-10-01','expected_project_revision':t['updated_at']},**ctx(s,w))
    value=s.reader.read('project_balance',resource_id=str(p))
    assert float(value['balance']['received_net'])==0 and value['expectations_in_received_net'] is False
    with pytest.raises(Rejected,match='actual_net_receipt'):
        execute(s.e,'set_payment_state',{'payment_status':'paid'},target_id=str(p),expected_revision=taskrow(s,p)['updated_at'],**ctx(s,w))

def test_workspace_server_list_move_preserves_uid(system):
    s=system;w=workspace(s);task=execute(s.e,'create_task',{'title':'Перенос'},**ctx(s,w))['id']
    with s.admin.transaction(read_only=True) as tx:
        b=tx.one('SELECT resource_id FROM tm_config.reminder_bindings WHERE task_id=%s',(task,));old=calendar.event(tx,s.owner,b['resource_id'])
    setting=s.reader.read('workspace',workspace_id=w)['workspace']
    execute(s.e,'update_workspace',{'reminder_list':{'mode':'server_new','title':'Другое назначение'}},target_id=w,expected_revision=setting['revision'])
    with s.admin.transaction(read_only=True) as tx:new=calendar.event(tx,s.owner,b['resource_id'])
    assert new['calendar_id']!=old['calendar_id'] and new['caldav_uid']==old['caldav_uid'] and new['id']==old['id']

def test_real_dav_todo_viewer_revoke_and_owner_completion(system):
    import httpx
    from tm_calendar.wsgi import application
    s=system;w=workspace(s);task=execute(s.e,'create_task',{'title':'CalDAV проверка'},**ctx(s,w))['id']
    with s.admin.transaction() as tx:
        calendar.lock(tx);b=tx.one('SELECT resource_id FROM tm_config.reminder_bindings WHERE task_id=%s',(task,));r=calendar.event(tx,s.owner,b['resource_id']);cid=r['calendar_id']
        calendar.member(tx,s.owner,cid,s.viewer,'viewer','active',0)
    with httpx.Client(transport=httpx.WSGITransport(application),base_url='http://calendar.test') as client:
        path=f'/{s.owner}/{cid}/{r["href"]}';view=f'/{s.viewer}/{cid}/{r["href"]}'
        got=client.get(path,auth=(s.owner,s.password));assert got.status_code==200 and 'BEGIN:VTODO' in got.text
        v=client.get(view,auth=(s.viewer,s.password));assert v.status_code==200
        deny=client.delete(view,headers={'If-Match':v.headers['etag']},auth=(s.viewer,s.password));assert deny.status_code in (401,403)
        body=got.text.replace('STATUS:NEEDS-ACTION','STATUS:COMPLETED')
        updated=client.put(path,content=body.encode(),headers={'If-Match':got.headers['etag'],'Content-Type':'text/calendar'},auth=(s.owner,s.password));assert updated.status_code in (200,204)
        assert taskrow(s,task)['status']=='completed'
        with s.admin.transaction() as tx:calendar.lock(tx);calendar.member(tx,s.owner,cid,s.viewer,'viewer','revoked',1)
        assert client.get(view,auth=(s.viewer,s.password)).status_code in (401,403,404)
        assert client.get(path,auth=(s.owner,s.password)).status_code==200

def test_old_writer_cannot_overwrite_server_managed_task(system):
    import psycopg
    s=system;w=workspace(s);task=execute(s.e,'create_task',{'title':'Guard'},**ctx(s,w))['id']
    with pytest.raises(psycopg.errors.RaiseException,match='server_managed_entity'):
        with s.admin.transaction() as tx:tx.execute('UPDATE public.tasks SET title=%s WHERE id=%s',('Native write',task))
