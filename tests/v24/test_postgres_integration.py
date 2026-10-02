"""Real PostgreSQL + real Radicale; never run against production or fake dependencies.

Use a NEW tm_v24_test database with the real V24 schema and Calendar component
columns. Hosted bootstrap uses only empty synthetic prerequisite tables; it does
not claim full V22/business-schema compatibility. Each test uses unique synthetic
identities; history is not deleted.
"""
import os,uuid,json
import pytest
pytest.importorskip('psycopg',reason='Real PostgreSQL driver required')
pytest.importorskip('icalendar',reason='Real iCalendar dependency required')
pytest.importorskip('radicale',reason='Real CalDAV server required')
from argon2 import PasswordHasher
from tm_api.v24.database import Database
from tm_api.v24.engine import Engine
from tm_api.v24.review import Reviews
from tm_api.v24.models import ReviewDraft,ReviewAnswer
from tm_api.v24.common import Rejected
from tm_calendar import repository as repo

USER=[{'kind':'user','confirmation_ref':'test-explicit-user-message','statement':'Я подтверждаю только синтетическое тестовое изменение.'}]
EVENT={'title':'Тест — созвон','start_at':'2026-10-01T15:00:00+05:00','end_at':'2026-10-01T16:00:00+05:00','timezone':'Asia/Yekaterinburg'}

@pytest.fixture
def setup(monkeypatch):
    dsn=os.environ.get('TM_V24_TEST_DSN')
    if not dsn or os.environ.get('TM_TEST_ALLOW_DATABASE_WRITES')!='YES':pytest.skip('Explicit disposable database opt-in required')
    admin=Database(dsn)
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT current_database() AS name')['name']=='tm_v24_test','Refusing a non-test database'
    instance='test-'+uuid.uuid4().hex;owner='a_'+uuid.uuid4().hex[:12];viewer='v_'+uuid.uuid4().hex[:12];password='Synthetic-test-'+uuid.uuid4().hex
    with admin.transaction() as tx:
        repo.lock(tx)
        for name,inst in [(owner,instance),(viewer,None)]:
            tx.execute('INSERT INTO tm_calendar.users(username,instance_id,password_hash) VALUES(%s,%s,%s)',(name,inst,PasswordHasher().hash(password)))
    api=Database(os.environ.get('TM_V24_TEST_API_DSN',dsn));engine=Engine(api,instance)
    key='create-calendar-'+uuid.uuid4().hex
    p=engine.preview(key,[{'operation':'create_calendar','dedupe_key':key,'changes':{'title':'Проверка'},'evidence':USER}])
    result=engine.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')
    calendar=result['results'][0]['record']['id']
    with admin.transaction() as tx:
        repo.lock(tx);repo.member(tx,owner,calendar,viewer,'viewer','active',0)
    return admin,engine,owner,viewer,password,calendar

def create_event(engine,calendar):
    key='test-event-'+uuid.uuid4().hex
    p=engine.preview(key,[{'operation':'create_calendar_event','calendar_id':calendar,'dedupe_key':key,'changes':EVENT,'evidence':USER}])
    return key,p,engine.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')

def test_exact_retry_and_cas(setup):
    admin,e,owner,viewer,pw,c=setup;key,p,result=create_event(e,c)
    assert e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')==result
    with pytest.raises(Rejected,match='collision'):e.apply(key,p['preview_id'],p['preview_digest'],True,'different-confirmation')
    event=result['results'][0]['record']
    mutation={'operation':'update_calendar_event','target_id':event['id'],'expected_revision':event['revision'],'changes':{'title':'Переназвать'},'evidence':USER}
    update=e.preview('test-update-'+uuid.uuid4().hex,[mutation])
    with admin.transaction() as tx:
        repo.lock(tx);tx.execute('UPDATE tm_calendar.events SET revision=revision+1 WHERE id=%s::uuid',(event['id'],))
    with pytest.raises(Rejected,match='revision'):e.apply(update['request_key'],update['preview_id'],update['preview_digest'],True,'test-user-confirmation')

def pending_event(engine, calendar):
    key='security-preview-'+uuid.uuid4().hex
    p=engine.preview(key,[{'operation':'create_calendar_event','calendar_id':calendar,
        'dedupe_key':key,'changes':EVENT,'evidence':USER}])
    return key,p

def assert_preview_has_no_effects(admin, calendar, key, preview):
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT count(*) AS n FROM tm_calendar.events WHERE calendar_id=%s::uuid',(calendar,))['n']==0
        assert tx.one('SELECT count(*) AS n FROM tm_v24.operations WHERE operation=%s AND request_key=%s',('apply',key))['n']==0
        assert tx.one('SELECT status FROM tm_v24.previews WHERE id=%s::uuid',(preview['preview_id'],))['status']=='preview'

def test_wrong_preview_digest_rolls_back_without_receipt_or_event(setup):
    admin,e,owner,viewer,pw,c=setup;key,p=pending_event(e,c)
    wrong=('a' if p['preview_digest'][0]!='a' else 'b')+p['preview_digest'][1:]
    with pytest.raises(Rejected,match='preview_digest_mismatch'):
        e.apply(key,p['preview_id'],wrong,True,'test-user-confirmation')
    assert_preview_has_no_effects(admin,c,key,p)
    assert e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')['applied']

def test_expired_preview_has_no_receipt_or_event(setup):
    admin,e,owner,viewer,pw,c=setup;key,p=pending_event(e,c)
    with admin.transaction() as tx:
        tx.execute("UPDATE tm_v24.previews SET expires_at=now()-interval '1 second' WHERE id=%s::uuid",(p['preview_id'],))
    with pytest.raises(Rejected,match='preview_expired'):
        e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')
    assert_preview_has_no_effects(admin,c,key,p)

def test_concurrent_exact_preview_replay_commits_once(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    admin,e,owner,viewer,pw,c=setup;key,p=pending_event(e,c)
    barrier=Barrier(2)
    def apply():
        barrier.wait(timeout=5)
        return e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(apply) for _ in range(2)]
        results=[future.result(timeout=20) for future in futures]
    assert results[0]==results[1] and results[0]['applied']
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT count(*) AS n FROM tm_calendar.events WHERE calendar_id=%s::uuid',(c,))['n']==1
        assert tx.one('SELECT count(*) AS n FROM tm_v24.operations WHERE operation=%s AND request_key=%s',('apply',key))['n']==1

def test_preview_payload_cannot_change_under_existing_request_key(setup):
    admin,e,owner,viewer,pw,c=setup;key,p=pending_event(e,c)
    changed={'operation':'create_calendar_event','calendar_id':c,'dedupe_key':key,
             'changes':EVENT|{'title':'Altered synthetic input'},'evidence':USER}
    with pytest.raises(Rejected,match='request_key_collision'):
        e.preview(key,[changed])
    assert_preview_has_no_effects(admin,c,key,p)

def test_api_role_cannot_read_password_hash_or_disable_append_only_audit(setup):
    import psycopg
    admin,e,owner,viewer,pw,c=setup
    with e.database.transaction(read_only=True) as tx:
        role=tx.one('SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_roles WHERE rolname=current_user')
        assert not any(role.values()),'Acceptance must use the nonprivileged API role'
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with e.database.transaction(read_only=True) as tx:
            tx.one('SELECT password_hash FROM tm_calendar.users LIMIT 1')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with e.database.transaction() as tx:
            tx.execute('ALTER TABLE tm_v24.audit DISABLE TRIGGER ALL')

@pytest.mark.parametrize('attempt',range(3))
def test_concurrent_conflicting_confirmation_has_one_winner(setup,attempt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    admin,e,owner,viewer,pw,c=setup;key,p=pending_event(e,c)
    barrier=Barrier(2)
    def apply(confirmation):
        barrier.wait(timeout=5)
        try:
            return e.apply(key,p['preview_id'],p['preview_digest'],True,confirmation)
        except Rejected as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(apply,'synthetic-confirmation-'+str(i)) for i in range(2)]
        results=[future.result(timeout=20) for future in futures]
    assert sum(isinstance(r,dict) and r['applied'] for r in results)==1
    assert sum(r=='request_key_collision' for r in results)==1
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT count(*) AS n FROM tm_calendar.events WHERE calendar_id=%s::uuid',(c,))['n']==1
        assert tx.one('SELECT count(*) AS n FROM tm_v24.operations WHERE operation=%s AND request_key=%s',('apply',key))['n']==1

def test_move_event_preserves_identity_and_updates_both_calendar_sync_logs(setup):
    admin,e,owner,viewer,pw,c1=setup
    key='create-calendar-destination-'+uuid.uuid4().hex
    p=e.preview(key,[{'operation':'create_calendar','dedupe_key':key,'changes':{'title':'Подтвержденные'},'evidence':USER}])
    c2=e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')['results'][0]['record']['id']
    _,_,created=create_event(e,c1)
    event=created['results'][0]['record']
    move_key='move-event-'+uuid.uuid4().hex
    mutation={'operation':'move_calendar_event','target_id':event['id'],'calendar_id':c2,
              'expected_revision':event['revision'],'changes':{},'evidence':USER}
    preview=e.preview(move_key,[mutation])
    result=e.apply(move_key,preview['preview_id'],preview['preview_digest'],True,'test-user-confirmation')
    moved=result['results'][0]['record']
    assert moved['id']==event['id']
    assert moved['caldav_uid']==event['caldav_uid']
    assert moved['href']==event['href']
    assert str(moved['calendar_id'])==str(c2)
    with admin.transaction(read_only=True) as tx:
        row=repo.event(tx,owner,event['id'])
        assert str(row['calendar_id'])==str(c2)
        old=tx.one('SELECT deleted FROM tm_calendar.changes WHERE calendar_id=%s::uuid AND href=%s ORDER BY revision DESC LIMIT 1',(c1,event['href']))
        new=tx.one('SELECT deleted FROM tm_calendar.changes WHERE calendar_id=%s::uuid AND href=%s ORDER BY revision DESC LIMIT 1',(c2,event['href']))
        assert old['deleted'] is True and new['deleted'] is False

def test_atomic_batch_rollback_and_no_orphan_receipt(setup):
    admin,e,owner,viewer,pw,c=setup
    key='test-batch-'+uuid.uuid4().hex
    mutations=[{'operation':'create_calendar_event','calendar_id':c,'dedupe_key':key+'-'+str(i),'changes':EVENT|{'title':str(i)},'evidence':USER} for i in (1,2)]
    p=e.preview(key,mutations)
    original=e.calendar.execute
    def second_fails(tx,m,plan):
        if m['changes']['title']=='2':raise Rejected('synthetic_second_failure')
        return original(tx,m,plan)
    e.calendar.execute=second_fails
    with pytest.raises(Rejected):e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')
    with admin.transaction(read_only=True) as tx:
        assert tx.one('SELECT count(*) AS n FROM tm_calendar.events WHERE calendar_id=%s::uuid',(c,))['n']==0
        assert tx.one('SELECT status FROM tm_v24.previews WHERE id=%s::uuid',(p['preview_id'],))['status']=='preview'
    assert not e.submission(key)['found']
    e.calendar.execute=original
    assert len(e.apply(key,p['preview_id'],p['preview_digest'],True,'test-user-confirmation')['results'])==2

def test_projectless_review_one_click_and_retry(setup):
    admin,e,owner,viewer,pw,c=setup
    key='review-preview-'+uuid.uuid4().hex
    p=e.preview(key,[{'operation':'create_calendar_event','calendar_id':c,'dedupe_key':key,'changes':EVENT,'evidence':USER}])
    draft=ReviewDraft(item_key='review-item-'+uuid.uuid4().hex,title='Встреча из переписки',context='Синтетический контекст',uncertainty='Нужна проверка',evidence=USER,
      available_actions=[{'action_id':'create','label':'Создать','intent':'apply','preview_id':p['preview_id'],'preview_digest':p['preview_digest']},{'action_id':'clarify','label':'Уточнить','intent':'clarify'}])
    reviews=Reviews(e);r=reviews.create('create-review-'+uuid.uuid4().hex,draft)
    v=reviews.show({'name':'Основная сверка Владельца','settings':{'layout':'cards'}})
    assert v['question']['id']==r['review_id'] and v['question']['group']==''
    answer=ReviewAnswer(**v['question']['options'][0]['answer'],confirmed=True,confirmation_ref='test-user-click')
    key='answer-review-'+uuid.uuid4().hex
    receipt=reviews.answer(key,answer)
    assert receipt['decision_saved'] and receipt['effect']['applied']
    assert reviews.answer(key,answer)==receipt
    assert reviews.current()['question'] is None

def test_real_dav_owner_viewer_conditional_writes_and_revoke(setup,monkeypatch):
    import httpx
    admin,e,owner,viewer,pw,c=setup
    # Use an actual WSGI server instance, not a mock permission check.
    from tm_calendar.wsgi import application
    key,p,result=create_event(e,c);event=result['results'][0]['record'];href=event['href']
    with httpx.Client(transport=httpx.WSGITransport(application),base_url='http://calendar.test') as client:
        a=client.get(f'/{owner}/{c}/{href}',auth=(owner,pw));assert a.status_code==200
        v=client.get(f'/{viewer}/{c}/{href}',auth=(viewer,pw));assert v.status_code==200 and v.text==a.text
        discovery=client.request('PROPFIND',f'/{viewer}/',headers={'Depth':'1'},auth=(viewer,pw));assert discovery.status_code==207 and c in discovery.text
        for method in ['PUT','DELETE']:
            r=client.request(method,f'/{viewer}/{c}/{href}',headers={'If-Match':v.headers['etag'],'Content-Type':'text/calendar'},content=v.content if method=='PUT' else b'',auth=(viewer,pw))
            assert r.status_code in (401,403),r.text
        unknown=client.put(f'/{viewer}/{c}/new.ics',headers={'If-None-Match':'*','Content-Type':'text/calendar'},content=v.content,auth=(viewer,pw));assert unknown.status_code in (401,403)
        # The owner edit is immediately canonical in PostgreSQL.
        changed=a.text.replace('Тест — созвон','Изменено владельцем')
        r=client.put(f'/{owner}/{c}/{href}',headers={'If-Match':a.headers['etag'],'Content-Type':'text/calendar'},content=changed.encode(),auth=(owner,pw));assert r.status_code in (200,204)
        stale=client.put(f'/{owner}/{c}/{href}',headers={'If-Match':a.headers['etag'],'Content-Type':'text/calendar'},content=a.content,auth=(owner,pw));assert stale.status_code==412
        missing=client.put(f'/{owner}/{c}/{href}',content=a.content,auth=(owner,pw));assert missing.status_code==428
        with admin.transaction(read_only=True) as tx:assert repo.event(tx,owner,event['id'])['fields']['title']=='Изменено владельцем'
        with admin.transaction() as tx:repo.lock(tx);repo.member(tx,owner,c,viewer,'viewer','revoked',1)
        assert client.get(f'/{viewer}/{c}/{href}',auth=(viewer,pw)).status_code in (401,403,404)
        assert client.get(f'/{owner}/{c}/{href}',auth=(owner,pw)).status_code==200
