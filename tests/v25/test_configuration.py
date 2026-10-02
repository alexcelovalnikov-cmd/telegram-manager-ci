"""Pure validation/rendering/planner tests. These do NOT claim DB/Apple acceptance."""
import copy,inspect,typing
from types import SimpleNamespace
import pytest
from pydantic import ValidationError,create_model,ConfigDict
from tm_api.v24.common import Rejected
from tm_api.v24.models import Mutation
from tm_api.v24.engine import Engine
from tm_api.v25.configuration import Workspace,Rule,Document,Template,EntityType,render,validate_entity,SourceSelector
from tm_api.v25.business import ConfigBusiness,CONFIG_OPS,ENTITY_OPS
from tm_api.v25.repository import assert_context,effective_template,present
from tm_api.v25.finance import cost,money,balance,display
from tm_reminders.models import Reminder
from tm_reminders.business import OPS as REMINDER_OPS

USER=[{'kind':'user','confirmation_ref':'test-user-confirmation','statement':'Явное синтетическое указание пользователя.'}]
ID='11111111-1111-4111-8111-111111111111'
FORMAT={'received_marker':'💶','partial_separator':' + ','thousands_suffix':'к','decimal_separator':',','currency_suffix':' ₽','tag_separator':' ','date_format':'MM.DD'}

@pytest.mark.parametrize('value',['NaN','Infinity','-1','0.001','1000000001',20,True,None])
def test_money_rejects_ambiguous_or_invalid(value):
    with pytest.raises(Rejected):money(value)

def test_tax_is_not_work_value():
    assert cost({'work_amount':'20000','tax_amount':'1200','document_total':'21200'})=={'work_amount':'20000','tax_amount':'1200','document_total':'21200'}
    assert cost({'work_amount':'20000'})['tax_amount'] is None

@pytest.mark.parametrize('data',[{'document_total':'21200'},{'work_amount':'20000','tax_amount':'1200','document_total':'20000'},{'work_amount':'20000','document_total':'19000'},{'work_amount':'20000','tax_rate':'0.06'}])
def test_no_assumed_tax_rate_or_conflicting_document(data):
    with pytest.raises(Rejected):cost(data)

def receipt(amount,kind='received',state='confirmed',valid=True):
    return {'amount':amount,'kind':kind,'state':state,'evidence_valid':valid}

def test_partial_payment_comes_from_ledger_not_title():
    f=balance('40000',[receipt('20000')])
    assert f['payment_status']=='partial' and f['remaining']=='20000'
    assert display(f,FORMAT)=='💶20к + 20к'
    fully=balance('40000',[receipt('20000'),receipt('20000')])
    assert fully['remaining']=='0' and fully['payment_status']=='paid'
    assert display(fully,FORMAT)=='40к'

def test_promise_unconfirmed_and_invalid_evidence_not_receipts():
    f=balance('40000',[receipt('20000','promised'),receipt('20000',state='pending'),receipt('20000',valid=False)])
    assert f['received_net']=='0' and f['remaining']=='40000'

def test_refunds_overpayments_and_unknown_cost_explicit():
    f=balance('40000',[receipt('50000'),receipt('5000','refund')])
    assert f['overpayment']=='5000' and f['remaining']=='0'
    assert balance(None,[receipt('20000')])['remaining'] is None
    assert balance('20000',[receipt('1000','refund')])['negative_receipts_require_review']

def test_payment_format_is_configuration():
    f=balance('40000',[receipt('20000')])
    assert display(f,FORMAT|{'received_marker':'Получено ','partial_separator':' / остаток '})=='Получено 20к / остаток 20к'

def test_prefix_hashtag_without_code_change():
    values={'title':'Созвон с Дмитрием','tags':'#созвон','description':'Контекст'}
    assert render('{title} {tags}',values)=='Созвон с Дмитрием #созвон'
    assert render('{tags} {title}',values)=='#созвон Созвон с Дмитрием'

@pytest.mark.parametrize('pattern',['{__class__}','{title.__class__}','{title[0]}','{title!r}','{title:10000000}','{x()}', '{title'])
def test_templates_never_execute_code_or_unbounded_formatting(pattern):
    with pytest.raises((Rejected,ValidationError)):Template(entity_kind='task',channel='title',pattern=pattern)

def test_template_unknown_field_fails_not_silently_empty():
    with pytest.raises(Rejected,match='missing'):render('{title} {private.token}',{'title':'x'})

def test_template_only_dictionary_paths_and_escaped_braces():
    assert render('{{{workspace.name}}} {title}',{'workspace':{'name':'RCC'},'title':'Тест'})=='{RCC} Тест'
    with pytest.raises(Rejected):render('{title}',{'title':object()})

@pytest.mark.parametrize('patch',[{'analysis':{'python':'eval()'}},{'formatting':{'command':'rm'}},{'default_tags':['x'*101]}, {'chat_ids':[1,1]},{'source_selectors':[{'kind':'ssh','value':'x'}]}])
def test_workspace_settings_are_closed_data(patch):
    with pytest.raises((ValidationError,Rejected)):Workspace(key='rcc',name='RCC',**patch)

def test_new_workspace_can_describe_new_chat_and_reminder_list():
    w=Workspace(key='postproduction',name='Постпродакшен',chat_ids=[1,2,3,4,5],reminder_list={'mode':'server_new','title':'Постпродакшен'},
       analysis={'instructions':'Предложенные даты не считать окончательными.','tracked_kinds':['versions','client_changes']},default_tags=['постпродакшен'],
       documents=[{'key':'deadline','kind':'rule','body':{'text':'Давайте к дате — предварительный срок.'}},
                  {'key':'task-title','kind':'template','body':{'entity_kind':'task','channel':'title','pattern':'{tags} {title}'}}])
    assert len(w.chat_ids)==5 and w.reminder_list.mode=='server_new'
    assert SourceSelector(kind='username',value='@post_client').value=='post_client'

def test_custom_model_does_not_need_python_workflow():
    spec=EntityType(label='Версия монтажа',fields={'version':{'type':'text'},'deadline':{'type':'date'},'approved':{'type':'boolean'}},required=['version'],states=['editing','approved'])
    data={'title':'Ролик','version':'V3','deadline':'2026-10-05','approved':False,'state':'editing'}
    assert validate_entity(spec.model_dump(),data)==data
    with pytest.raises(Rejected):validate_entity(spec.model_dump(),data|{'state':'paid'})
    with pytest.raises(Rejected):validate_entity(spec.model_dump(),data|{'python':'run me'})

def context():
    local={'id':ID,'workspace_id':ID,'revision':2,'status':'active','kind':'rule','body':{'text':'Правило'}}
    return {'workspace':{'id':ID},'configuration_token':'a'*64,'active_rules':[local]}

def test_current_config_and_exact_rule_version_required():
    c=context();m={'workspace_id':ID,'configuration_token':'a'*64,'analysis':{'rule_refs':[{'id':ID,'revision':2}],'reason':'Применено проектное правило'}}
    assert_context(c,m)
    for patch in [{'configuration_token':'b'*64},{'workspace_id':'another'},{'analysis':{'rule_refs':[{'id':ID,'revision':1}]}}]:
        with pytest.raises(Rejected):assert_context(c,m|patch)

def test_project_template_does_not_replace_other_project():
    template=lambda wid,pattern:{'workspace_id':wid,'status':'active','kind':'template','body':{'entity_kind':'task','channel':'title','pattern':pattern}}
    c={'workspace':{'id':ID},'documents':[template(None,'{title}'),template(ID,'{tags} {title}')],'defaults':{'templates':{'task':{'title':'{title}','description':'{description}'}}}}
    assert effective_template(c,'task','title')=='{tags} {title}'
    c['workspace']['id']='other'
    assert effective_template(c,'task','title')=='{title}'

class DocumentTx:
    """Planner-only fake: no persistence or transaction guarantees are simulated."""
    def __init__(self,existing=None,conflicts=()):self.existing=existing;self.conflicts=conflicts
    def one(self,sql,args=()):
        if 'SELECT * FROM tm_config.documents' in sql:return copy.deepcopy(self.existing)
        if 'SELECT id FROM tm_config.documents' in sql:return None
        raise AssertionError(sql)
    def all(self,sql,args=()):
        if 'SELECT id,kind,body FROM tm_config.documents' in sql:return list(self.conflicts)
        raise AssertionError(sql)

def mutate(op,changes,**kwargs):
    d={'operation':op,'changes':changes,'evidence':USER,**kwargs}
    if op.startswith('create_'):d.setdefault('dedupe_key','test-candidate-'+op)
    else:d.setdefault('target_id',ID);d.setdefault('expected_revision',1)
    return Mutation.model_validate(d).model_dump(mode='json')

def test_learned_generalization_cannot_self_activate():
    m=mutate('create_rule',{'key':'deadline','body':{'text':'Предложения дат предварительны.'},'origin':'review_generalization','status':'active','review_id':'review-test-1'})
    p=ConfigBusiness('test').plan(DocumentTx(),m)
    assert p['after']['status']=='proposed'
    assert p['after']['origin']=='review_generalization'

def rule_row():
    return {'id':ID,'instance_id':'test','workspace_id':None,'document_key':'deadline','kind':'rule','body':Rule(text='Предложения дат предварительны.').model_dump(),
            'status':'proposed','origin':'review_generalization','review_id':'test-review','evidence':USER,'revision':1}

def test_activation_is_a_separate_revision_and_requires_user_evidence():
    row=rule_row();m=mutate('activate_rule',{})
    p=ConfigBusiness('test').plan(DocumentTx(row),m)
    assert p['before']['status']=='proposed' and p['after']['status']=='active' and p['after']['revision']==2
    m['evidence']=[{'kind':'telegram','chat_id':1,'message_id':2,'content_token':'a'*32}]
    with pytest.raises(Rejected,match='direct_user'):ConfigBusiness('test').plan(DocumentTx(row),m)

def test_structural_conflict_requires_explicit_resolution():
    old={'id':'old','kind':'rule','body':{'decision_key':'deadline.proposed','value':True}}
    m=mutate('create_rule',{'key':'other','body':{'text':'Другой способ трактовки.','decision_key':'deadline.proposed','value':False}})
    with pytest.raises(Rejected,match='conflict'):ConfigBusiness('test').plan(DocumentTx(conflicts=[old]),m)

def test_disabled_rule_keeps_identity_and_history_revision():
    row=rule_row();row['status']='active'
    p=ConfigBusiness('test').plan(DocumentTx(row),mutate('disable_rule',{}))
    assert p['after']['id']==ID and p['after']['revision']==2 and p['after']['status']=='disabled'
    assert row['status']=='active'

@pytest.mark.parametrize('op',['archive_workspace','delete_workspace','restore_workspace','disable_rule','activate_rule','delete_rule','delete_template','complete_reminder','delete_reminder','delete_reminder_list'])
def test_lifecycle_mutations_still_require_cas(op):
    assert mutate(op,{})['expected_revision']==1
    with pytest.raises(ValidationError):Mutation.model_validate({'operation':op,'target_id':ID,'evidence':USER,'changes':{}})

def test_extension_is_opt_in_and_does_not_expose_raw_sql():
    m=mutate('create_workspace',{'key':'post','name':'Post'})
    with pytest.raises(Rejected,match='disabled'):Engine(None,'test').planner(m)
    assert type(Engine(None,'test',True).planner(m)).__name__=='ConfigBusiness'
    with pytest.raises(ValidationError):Mutation.model_validate(m|{'operation':'execute_sql'})

def test_workspace_storage_migration_is_explicit_configuration_operation():
    m=mutate('migrate_workspace_storage',{'reminder_list_title':'Проекты','migrate_calendar':True})
    assert m['operation'] in CONFIG_OPS
    assert type(Engine(None,'test',True).planner(m)).__name__=='ConfigBusiness'
    assert m['target_id']==ID and m['expected_revision']==1

def test_bound_reminder_category_normalization_is_narrow_operation():
    m=mutate('normalize_reminder_categories',{'categories':[]})
    assert m['operation'] in REMINDER_OPS
    assert type(Engine(None,'test',True).planner(m)).__name__=='RemindersBusiness'
    with pytest.raises(ValidationError):
        Mutation.model_validate(m|{'changes':{}})

@pytest.mark.parametrize('patch',[{'due_at':'2026-10-01T12:00:00'},{'timezone':'invalid/zone'},{'priority':10},{'status':'paid'},{'recurrence':'FREQ=DAILY\nUID:other'},{'categories':['']}])
def test_reminder_validation(patch):
    with pytest.raises((ValueError,KeyError)):Reminder(title='Проверить монтаж',**patch)

def test_no_time_reminder_and_all_day_date_supported():
    assert Reminder(title='Без срока').due_at is None
    r=Reminder(title='Дедлайн',due_at='2026-10-01',all_day=True)
    assert r.due_at=='2026-10-01' and r.all_day

def test_new_read_tools_have_closed_input_schemas():
    from tm_api.v25.register import register,READ_NAMES
    operations={}
    def add(fn):operations[fn.__name__]=fn;return fn
    register(SimpleNamespace(dynamic=SimpleNamespace()),add,None)
    assert set(operations)==set(READ_NAMES)
    for name,fn in operations.items():
        hints=typing.get_type_hints(fn,include_extras=True)
        fields={p.name:(hints[p.name],... if p.default is inspect.Parameter.empty else p.default) for p in inspect.signature(fn).parameters.values()}
        model=create_model(name,__config__=ConfigDict(extra='forbid',strict=True),**fields)
        assert model.model_json_schema()['additionalProperties'] is False
