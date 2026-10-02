"""Local unit tests. These do not stand in for PostgreSQL/DAV integration."""
import copy
from contextlib import contextmanager
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from pydantic import ValidationError,TypeAdapter
from tm_api.v24.models import Mutation,Search,ReviewDraft,ReviewAnswer,Event
from tm_api.v24.engine import Engine
from tm_api.write_models import ReconciliationPaymentWindow
from tm_api.v24.common import Rejected,encoded,digest,diff,strict_fields,check_revision,make_cursor,read_cursor
from tm_api.v24.business import Business
from tm_api.v24.scope import evidence
from tm_calendar import repository
from tm_api.config import Config

USER=[{'kind':'user','confirmation_ref':'chat-user-message-1','statement':'Исправь согласно показанному изменению.'}]
BASE={'operation':'update_project','target_id':'12','expected_revision':'2026-09-28T00:00:00+00:00','changes':{'label':'ДемоКлиент'},'evidence':USER}

@pytest.mark.parametrize('patch',[{'target_id':None},{'expected_revision':None},{'target_id':'-1'},{'target_id':'12;drop'},{'operation':'raw_sql'},{'evidence':[]},{'confirmed':True},{'changes':{}}])
def test_mutation_rejects_unguarded_inputs(patch):
    with pytest.raises(ValidationError):Mutation.model_validate(BASE|patch)

def test_create_independent_of_project_and_review():
    m=Mutation(operation='create_task',group_key='rcc',dedupe_key='promise-chat-12-msg-23',changes={'title':'Проверить'},evidence=USER)
    assert m.target_id is None and m.expected_revision is None

TELEGRAM=[{'kind':'telegram','chat_id':266334332,'message_id':348580,'content_token':'token-vk-payment'}]

def test_reconciliation_payment_window_is_telegram_only_and_rule_bound():
    value=ReconciliationPaymentWindow(
        project_id=43,
        expected_updated_at='2026-09-29T04:22:29+00:00',
        window={'kind':'relative','within_days':7},
        evidence=TELEGRAM,
        finding_key='reconciliation:266334332:348580:payment',
        rule_key='payment_lightning_two_week_rule',
        rationale='Дмитрий однозначно обещал оплату на этой неделе.',
    )
    assert value.evidence[0].kind=='telegram'
    with pytest.raises(ValidationError):
        ReconciliationPaymentWindow(
            project_id=43,
            expected_updated_at='2026-09-29T04:22:29+00:00',
            window={'kind':'relative','within_days':7},
            evidence=USER,
            finding_key='reconciliation:266334332:348580:payment',
            rule_key='payment_lightning_two_week_rule',
            rationale='Недопустимая подмена Telegram evidence.',
        )
    with pytest.raises(ValidationError):
        ReconciliationPaymentWindow(
            project_id=43,
            expected_updated_at='2026-09-29T04:22:29+00:00',
            window={'kind':'relative','within_days':7},
            evidence=TELEGRAM,
            finding_key='reconciliation:266334332:348580:payment',
            rule_key='invented_rule',
            rationale='Нельзя придумать новое правило.',
        )

class ReconciliationDb:
    @contextmanager
    def transaction(self, read_only=False):
        yield object()

def reconciliation_engine(payload):
    engine=Engine.__new__(Engine)
    engine.database=ReconciliationDb()
    engine.instance='instance'
    engine.transaction_lock=lambda tx,write: None
    engine.receipt=lambda tx,operation,key,arguments: None
    engine.get_preview=lambda tx,preview_id: {
        'id':preview_id,'request_key':'reconciliation-preview-0001','payload':payload,
    }
    engine.apply_in_transaction=lambda tx,preview_id,preview_digest,confirmation_ref: {
        'applied':True,'preview_id':preview_id,'confirmation_ref':confirmation_ref,
    }
    engine.saved=None
    engine.save_receipt=lambda tx,operation,key,arguments,result: setattr(engine,'saved',(operation,result))
    return engine

def test_reconciliation_apply_allows_bound_deterministic_operational_change():
    finding='reconciliation:406437132:348590:task'
    rule='reply_followup_rule'
    payload=[{
        'operation':'update_task',
        'evidence':TELEGRAM,
        'analysis':{'classification':'deterministic','finding_key':finding,'rule_key':rule},
    }]
    engine=reconciliation_engine(payload)
    result=engine.apply_reconciliation(
        'reconciliation-preview-0001','00000000-0000-0000-0000-000000000010','a'*64,
        finding,rule,'Однозначное действие по существующему правилу.')
    assert result['authorization']=='reconciliation_standing_policy'
    assert result['confirmation_ref']=='reconciliation:'+finding
    assert engine.saved[0]=='reconciliation_apply'

@pytest.mark.parametrize('payload,error',[
    ([{'operation':'create_payment','evidence':TELEGRAM,
       'analysis':{'classification':'deterministic','finding_key':'reconciliation:1:2:test','rule_key':'rule'}}],
     'requires_user_confirmation'),
    ([{'operation':'update_task','evidence':USER,
       'analysis':{'classification':'deterministic','finding_key':'reconciliation:1:2:test','rule_key':'rule'}}],
     'telegram_evidence'),
    ([{'operation':'update_task','evidence':TELEGRAM,
       'analysis':{'classification':'ambiguous','finding_key':'reconciliation:1:2:test','rule_key':'rule'}}],
     'analysis_binding'),
])
def test_reconciliation_apply_rejects_nonstanding_policy_inputs(payload,error):
    engine=reconciliation_engine(payload)
    with pytest.raises(Rejected,match=error):
        engine.apply_reconciliation(
            'reconciliation-preview-0001','00000000-0000-0000-0000-000000000010','a'*64,
            'reconciliation:1:2:test','rule','Не применять без нужных гарантий.')

@pytest.mark.parametrize('filters',[{}, {'query':''},{'sender_id':-1,'chat_ids':[-123]},{'date_from':'2026-09-14T00:00:00+05:00','date_to':'2026-09-28T00:00:00+05:00'}])
def test_global_search_does_not_require_a_project(filters):
    assert Search(**filters).project_id is None

@pytest.mark.parametrize('filters',[{'limit':101},{'chat_ids':[]},{'date_from':'2026-09-28'},{'date_from':'2026-09-28T00:00:00Z','date_to':'2026-09-27T00:00:00Z'},{'limit':True}])
def test_bounded_search(filters):
    with pytest.raises(ValidationError):Search(**filters)

def test_diff_null_missing_arrays_boolean_and_escaping():
    assert diff({}, {'a':None})==[{'op':'add','path':'/a','after':None}]
    assert diff({'a':None}, {})==[{'op':'remove','path':'/a','before':None}]
    assert diff(True,1)
    assert diff({'a/b~':[1]},{'a/b~':[2]})[0]['path']=='/a~1b~0'
    assert diff({'x':1},{'x':1})==[]

@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity'])
def test_no_nonfinite_json(value):
    with pytest.raises(ValueError):encoded({'x':float(value)})

def test_cursor_cannot_cross_scope_or_be_tampered():
    token=make_cursor({'scope':'rcc','position':[1,2,3]},'test-secret')
    assert read_cursor(token,'test-secret','rcc')['position']==[1,2,3]
    for secret,scope,t in [('other','rcc',token),('test-secret','foreign',token),('test-secret','rcc',token+'a')]:
        with pytest.raises(Rejected):read_cursor(t,secret,scope)

def test_cas_equivalent_timestamp_only():
    check_revision('2026-09-28T00:00:00Z','2026-09-28T05:00:00+05:00')
    with pytest.raises(Rejected):check_revision(3,4)
    with pytest.raises(Rejected):check_revision('2026-09-28T00:00:00Z','2026-09-28T00:00:00')

@pytest.mark.parametrize('patch',[{'password':'secret'}, {'payment_status':'paid'}, {'context_group_id':5}, {'title':'a'*29000}])
def test_patch_whitelist(patch):
    with pytest.raises(Rejected):strict_fields(patch,['title'])

EVENT={'title':'Созвон','start_at':'2026-10-01T15:00:00+05:00','end_at':'2026-10-01T16:00:00+05:00'}
@pytest.mark.parametrize('patch',[{'end_at':'2026-10-01T14:00:00+05:00'},{'start_at':'2026-10-01T15:00:00'},{'timezone':'Bad/Zone'},{'recurrence':'FREQ=DAILY\nUID:forged'}])
def test_event_rejects_ambiguous_dates(patch):
    with pytest.raises(ValidationError):Event(**(EVENT|patch))

def test_all_day_is_date_not_midnight():
    e=Event(title='Съёмка',all_day=True,start_at='2026-10-01',end_at='2026-10-02')
    assert e.start_at=='2026-10-01'
    with pytest.raises(ValidationError):Event(title='Съёмка',all_day=True,start_at='2026-10-01',end_at='2026-10-01')

def test_projectless_review_and_real_button_binding():
    args={'item_key':'promise-candidate-0001','title':'Проверить материалы','context':'Обещал вечером','uncertainty':'Подтверждения выполнения нет','evidence':USER,
        'available_actions':[{'action_id':'clarify','label':'Уточнить','intent':'clarify'}]}
    assert ReviewDraft(**args).group_key is None
    with pytest.raises(ValidationError):ReviewDraft(**(args|{'available_actions':[{'action_id':'add','label':'Добавить в дела','intent':'apply'}]}))

@pytest.mark.parametrize('confirmed',[False,1,'true'])
def test_click_confirmation_cannot_be_coerced(confirmed):
    with pytest.raises(ValidationError):ReviewAnswer(review_id='00000000-0000-0000-0000-000000000001',revision=1,focus_version=1,action_id='add',confirmed=confirmed,confirmation_ref='explicit-ui-click')

class PlanTx:
    def __init__(self,row,salary=False):self.row=copy.deepcopy(row);self.salary=salary
    def one(self,sql,args=()):
        if 'FROM public.tasks t JOIN' in sql:return copy.deepcopy(self.row)
        if 'FROM public.telegram_chat_groups' in sql:return {'id':7,'group_key':'projects','rules_profile':'projects_payments','reminder_list_id':'list'}
        if 'FROM public.tm_salary_' in sql:return {'found':1} if self.salary else None
        if 'tm_payment_title_v18' in sql:return {'title':args[0]}
        if 'FROM public.tasks WHERE' in sql:return None
        raise AssertionError(sql)
    def all(self,*args):raise AssertionError(args)
    def execute(self,*args):raise AssertionError(args)

PROJECT={'id':12,'context_group_id':7,'record_kind':'project','title':'09.16 Демо Клиент - 15к + 5k','description':'',
 'status':'completed','updated_at':'2026-09-28T00:00:00+00:00','project_archived_at':None,'target_app':'reminders',
 'project_data':{'label':'Демо Клиент','date_mmdd':'09.16','amount_rub':'20000.00'},'payment_status':'unknown','payment_evidence':None,'payment_confirmed_at':None}

def test_cc_direct_user_evidence_no_focus_or_telegram():
    m=Mutation.model_validate(BASE|{'changes':{'amount_expression':'15к + CC 5k'}}).model_dump(mode='json')
    plan=Business('macbook-owner').plan(PlanTx(PROJECT),m)
    assert plan['after']['title']=='09.16 Демо Клиент - 15к + CC 5k'
    assert plan['after']['project_data']['amount_rub']=='20000.00'
    assert plan['after']['status']=='completed' and plan['after']['payment_status']=='unknown'
    assert PROJECT['title'].endswith('15к + 5k')

def test_project_notes_do_not_break_native_no_notes_rule():
    m=Mutation.model_validate(BASE|{'changes':{'description':'Подробности только на сервере'}}).model_dump(mode='json')
    plan=Business('macbook-owner').plan(PlanTx(PROJECT),m)
    assert plan['after']['description']==''
    assert plan['after']['project_data']['server_description']=='Подробности только на сервере'

def test_salary_cannot_be_edited_generically():
    with pytest.raises(Rejected,match='salary'):Business('macbook-owner').plan(PlanTx(PROJECT,True),Mutation.model_validate(BASE).model_dump(mode='json'))

def test_paid_is_independent_and_does_not_authorize_removal():
    m=Mutation.model_validate(BASE|{'operation':'set_payment_state','changes':{'payment_status':'paid'}}).model_dump(mode='json')
    result=Business('macbook-owner').plan(PlanTx(PROJECT),m)['after']
    assert result['status']=='completed'
    assert result['payment_evidence']['removal_authorized'] is False
    assert result['payment_confirmed_at']=='assigned_at_commit'

def test_paid_cannot_be_reverted_by_generic_status():
    m=Mutation.model_validate(BASE|{'operation':'set_payment_state','changes':{'payment_status':'unknown'}}).model_dump(mode='json')
    with pytest.raises(Rejected,match='reconciliation'):Business('macbook-owner').plan(PlanTx(PROJECT|{'payment_status':'paid'}),m)

class ACLTx:
    def __init__(self,role='viewer',active=True,member=True):self.role=role;self.active=active;self.member=member
    def one(self,sql,args=()):
        if 'FROM tm_calendar.users' in sql:return {'username':'user_1','active':self.active,'revision':1,'instance_id':None}
        if 'JOIN tm_calendar.members' in sql:return {'id':'calendar','role':self.role} if self.member else None
        raise AssertionError(sql)

@pytest.mark.parametrize('role',['owner','viewer','editor'])
def test_members_read(role):assert repository.access(ACLTx(role),'user_1','calendar')['role']==role
@pytest.mark.parametrize('role',['owner','editor'])
def test_only_writers_write(role):assert repository.access(ACLTx(role),'user_1','calendar',write=True)
@pytest.mark.parametrize('write,manage',[(True,False),(False,True),(True,True)])
def test_viewer_denied_all_writes(write,manage):
    with pytest.raises(Rejected):repository.access(ACLTx(),'user_1','calendar',write,manage)
@pytest.mark.parametrize('active,member',[(False,True),(True,False),(False,False)])
def test_revoked_or_suspended_access(active,member):
    with pytest.raises(Rejected):repository.access(ACLTx('owner',active,member),'user_1','calendar')
@pytest.mark.parametrize('name',['../root','a/b','a%2fb','',None,'a'*65])
def test_bad_usernames(name):
    with pytest.raises(Rejected):repository.username(name)

def test_dynamic_feature_disabled_by_default_and_dsn_not_in_repr():
    c=Config('http://db-rest:3000','test','0'*64,v24_dsn='private-secret')
    assert not c.dynamic_enabled and 'private-secret' not in repr(c)

def test_registry_write_scope_covers_all_mutations():
    from tm_api.v24.register import WRITE_NAMES,MUTATION_NAMES,READ_NAMES
    assert set(MUTATION_NAMES)<=set(WRITE_NAMES)
    assert not set(WRITE_NAMES)&set(READ_NAMES)

def test_every_registered_tool_has_closed_pydantic_schema():
    import inspect,typing
    from pydantic import create_model,ConfigDict
    from tm_api.v24.register import register,READ_NAMES,WRITE_NAMES
    operations={}
    def add(fn):operations[fn.__name__]=fn;return fn
    register(SimpleNamespace(dynamic=SimpleNamespace(),config=SimpleNamespace(writes_enabled=True)),add,add,None)
    assert set(operations)==set(READ_NAMES+WRITE_NAMES)
    for name,fn in operations.items():
        hints=typing.get_type_hints(fn,include_extras=True)
        fields={p.name:(hints[p.name],... if p.default is inspect.Parameter.empty else p.default) for p in inspect.signature(fn).parameters.values()}
        model=create_model(name+'Input',__config__=ConfigDict(extra='forbid',strict=True),**fields)
        assert model.model_json_schema()['additionalProperties'] is False
