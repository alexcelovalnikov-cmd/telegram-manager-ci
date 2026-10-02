import pytest
from pydantic import ValidationError

from tm_api.v75.population import (
    PopulationItemCandidate,
    build_population_proposal,
)
from tm_api.v75.settlement import PROJECT_TYPE, ITEM_TYPE, PAYMENT_TYPE


CONTEXT={
    'instance_id':'instance',
    'workspace_id':'00000000-0000-4000-8000-000000000001',
    'workspace_key':'rcc',
    'spreadsheet_id':'18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk',
    'sheet_name':'Лист1',
    'write_mode':'observe',
    'identity_chat_ids':[100],
}


def evidence(message_id, token, role='project_identity'):
    return {'chat_id':100,'message_id':message_id,'content_token':token,
            'role':role,'attachment_id':None}


def evidence_row(message_id, token):
    return {'chat_id':100,'message_id':message_id,'content_token':token,
            'sender_name':'Клиент А','date':'2026-09-01T12:00:00+05:00',
            'text_preview':'Проект и смета','attachment_id':None}
def sheet(project_title='12 сентября Челябинск', item_title='Съемка'):
    return [
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        [project_title,'','',False,'',''],
        ['',item_title,30000,True,'обещано переводом',7],
        ['Итого:',0],
    ]


def candidates(project_title='RCC Челябинск'):
    return [{
        'sheet_row':2,'title':project_title,'date_iso':'2026-09-12',
        'identity_evidence':evidence(10,'project-token'),
        'evidence':[],
        'items':[{
            'sheet_row':3,'title':'Съемка','category':'shoot','amount_rub':'30000',
            'client_settlement':True,'personal_income':True,'pass_through':False,
            'order':10,'identity_evidence':evidence(11,'item-token','estimate'),
            'evidence':[],
        }],
    }]


def source_evidence():
    return [evidence_row(10,'project-token'),evidence_row(11,'item-token')]


def test_population_proposal_is_deterministic_and_never_creates_receipts():
    first=build_population_proposal(sheet(),[],candidates(),source_evidence(),CONTEXT)
    second=build_population_proposal(sheet(),[],candidates(),source_evidence(),CONTEXT)
    assert first==second
    assert first['safe_to_populate'] is True
    assert first['sheet_mutations']==[]
    assert first['counts']=={'projects':1,'items':1}
    assert all(x['entity_type']!=PAYMENT_TYPE for x in first['entities'])
    assert first['guards']['receipt_inference_forbidden'] is True
    item=next(x for x in first['entities'] if x['entity_type']==ITEM_TYPE)
    assert item['data']['amount_rub']=='30000.00'
    assert 'payment_type' not in item['data']
    assert item['binding']['sheet_paid_ignored'] is True
    assert item['binding']['sheet_payment_type_ignored'] is True
    project=next(x for x in first['entities'] if x['entity_type']==PROJECT_TYPE)
    assert project['binding']['legacy_title']=='12 сентября Челябинск'
    assert project['binding']['legacy_title_preserved'] is True


def test_rerun_after_exact_population_matches_existing_instead_of_duplicate_create():
    first=build_population_proposal(sheet(),[],candidates(),source_evidence(),CONTEXT)
    canonical=[
        {'id':x['id'],'entity_type':x['entity_type'],'data':x['data'],
         'status':'active','revision':1}
        for x in first['entities']
    ]
    second=build_population_proposal(sheet(),canonical,candidates(),source_evidence(),CONTEXT)
    assert second['safe_to_populate'] is True
    assert {x['action'] for x in second['entities']}=={'match_existing'}
    assert [x['id'] for x in first['entities']]==[x['id'] for x in second['entities']]


def test_existing_canonical_title_conflict_fails_closed_without_overwrite():
    canonical=[{
        'id':'00000000-0000-4000-8000-000000000999',
        'entity_type':PROJECT_TYPE,
        'data':{'title':'RCC Челябинск','date_iso':'2026-09-10'},
        'status':'active','revision':4,
    }]
    value=build_population_proposal(sheet(),canonical,candidates(),source_evidence(),CONTEXT)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_existing_project_title_conflict'
               for x in value['blockers'])
    assert all(x['action']!='update' for x in value['entities'])
@pytest.mark.parametrize('legacy_title,canonical_title',[
    ('Монтаж 6 роликов POV июль','RCC POV июль'),
    ('Готово боев','Готово боев'),
])
def test_excluded_projects_are_blocked(legacy_title,canonical_title):
    value=build_population_proposal(
        sheet(legacy_title),[],candidates(canonical_title),source_evidence(),CONTEXT)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_excluded_project' for x in value['blockers'])


def test_stale_telegram_content_token_blocks_population():
    stale=candidates()
    stale[0]['identity_evidence']['content_token']='old-token'
    value=build_population_proposal(sheet(),[],stale,source_evidence(),CONTEXT)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_evidence_token_changed' for x in value['blockers'])


def test_project_identity_cannot_be_date_only_or_scope_only():
    value=candidates()
    value[0]['identity_evidence']['role']='scope'
    result=build_population_proposal(sheet(),[],value,source_evidence(),CONTEXT)
    assert result['safe_to_populate'] is False
    assert any(x['code']=='population_project_identity_evidence_required'
               for x in result['blockers'])


def test_item_candidate_contract_forbids_received_money_fields():
    payload=candidates()[0]['items'][0] | {'payment_type':'cash'}
    with pytest.raises(ValidationError):
        PopulationItemCandidate.model_validate(payload)
def test_duplicate_candidate_project_identity_is_blocked():
    duplicated=candidates()+candidates()
    value=build_population_proposal(sheet(),[],duplicated,source_evidence(),CONTEXT)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_duplicate_candidate_entity'
               for x in value['blockers'])


def test_managed_write_mode_is_not_accepted_as_population_basis():
    context=CONTEXT | {'write_mode':'managed'}
    value=build_population_proposal(sheet(),[],candidates(),source_evidence(),context)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_requires_observe_mode' for x in value['blockers'])


def test_population_read_is_registered_in_configuration_surface():
    from tm_api.v25.register import READ_NAMES
    assert 'get_rcc_settlement_population_proposal' in READ_NAMES


def test_population_service_contract_is_exposed():
    from pathlib import Path
    text=Path('tm_api/service.py').read_text()
    assert "'rcc_settlement_population'" in text
    assert 'async def get_rcc_settlement_population_proposal' in text


def test_project_identity_must_use_alexandra_source_chat():
    value=candidates()
    value[0]['identity_evidence']['chat_id']=101
    rows=source_evidence()+[
        {'chat_id':101,'message_id':10,'content_token':'project-token',
         'sender_name':'Другой RCC контакт','date':'2026-09-01T12:00:00+05:00',
         'text_preview':'тот же день','attachment_id':None}]
    result=build_population_proposal(sheet(),[],value,rows,CONTEXT)
    assert result['safe_to_populate'] is False
    assert any(x['code']=='population_project_identity_not_alexandra_source'
               for x in result['blockers'])


def test_proposed_ids_match_existing_managed_entity_create_algorithm():
    from tm_api.v25.business import stable
    result=build_population_proposal(sheet(),[],candidates(),source_evidence(),CONTEXT)
    for entity in result['entities']:
        assert entity['id']==stable(CONTEXT['instance_id'],entity['dedupe_key'])


def test_proposed_entities_validate_against_production_shaped_rcc_schemas():
    schemas={
        PROJECT_TYPE:{
            'label':'RCC settlement project',
            'fields':{
                'date_iso':{'type':'date','label':'Дата'},
                'personal_project_id':{'type':'integer','label':'Проект в Проекты'},
            },
            'states':['active','cancelled'],'required':[],
            'reminder_projection':False,
        },
        ITEM_TYPE:{
            'label':'RCC settlement item',
            'fields':{
                'order':{'type':'integer','label':'Порядок'},
                'category':{'type':'text','label':'Категория','max_length':100},
                'performer':{'type':'text','label':'Исполнитель','max_length':300},
                'recipient':{'type':'text','label':'Получатель транзита','max_length':300},
                'amount_rub':{'type':'decimal','label':'Сумма'},
                'pass_through':{'type':'boolean','label':'Транзит'},
                'personal_income':{'type':'boolean','label':'Личный доход'},
                'client_settlement':{'type':'boolean','label':'В отчете Клиенту А'},
            },
            'states':['active','cancelled'],
            'required':['amount_rub','client_settlement','personal_income'],
            'reminder_projection':False,
        },
    }
    result=build_population_proposal(
        sheet(),[],candidates(),source_evidence(),CONTEXT,entity_schemas=schemas)
    assert result['safe_to_populate'] is True
    assert not any(x['code'].startswith('population_entity_') for x in result['blockers'])


def test_population_module_has_no_google_sheet_write_calls():
    from pathlib import Path
    text=Path('tm_api/v75/population.py').read_text()
    assert '.write_values(' not in text
    assert '.structural(' not in text


def test_existing_same_parent_item_title_blocks_even_when_amount_changed():
    first=build_population_proposal(sheet(),[],candidates(),source_evidence(),CONTEXT)
    project=next(x for x in first['entities'] if x['entity_type']==PROJECT_TYPE)
    canonical=[
        {'id':project['id'],'entity_type':PROJECT_TYPE,'data':project['data'],
         'status':'active','revision':1},
        {'id':'00000000-0000-4000-8000-000000000998',
         'entity_type':ITEM_TYPE,
         'data':{'parent_id':project['id'],'title':'Съемка','category':'shoot',
                 'amount_rub':'20000.00','client_settlement':True,
                 'personal_income':True,'pass_through':False,'order':10},
         'status':'active','revision':2},
    ]
    result=build_population_proposal(
        sheet(),canonical,candidates(),source_evidence(),CONTEXT)
    assert result['safe_to_populate'] is False
    assert any(x['code']=='population_existing_item_conflict'
               for x in result['blockers'])


def test_duplicate_candidate_item_title_blocks_even_with_different_evidence_and_amount():
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['12 сентября Челябинск','','',False,'',''],
        ['', 'Съемка',30000,False,'',''],
        ['', 'Съемка',10000,False,'',''],
        ['Итого:',40000],
    ]
    value=candidates()
    value[0]['items'].append({
        'sheet_row':4,'title':'Съемка','category':'shoot','amount_rub':'10000',
        'client_settlement':True,'personal_income':True,'pass_through':False,
        'order':20,'identity_evidence':evidence(12,'item-token-2','estimate'),
        'evidence':[],
    })
    source=source_evidence()+[evidence_row(12,'item-token-2')]
    result=build_population_proposal(rows,[],value,source,CONTEXT)
    assert result['safe_to_populate'] is False
    assert any(x['code']=='population_duplicate_candidate_item'
               for x in result['blockers'])


def test_ambiguous_alexandra_identity_source_blocks_population():
    context=CONTEXT | {'identity_chat_ids':[100,101]}
    value=build_population_proposal(sheet(),[],candidates(),source_evidence(),context)
    assert value['safe_to_populate'] is False
    assert any(x['code']=='population_identity_source_ambiguous' for x in value['blockers'])


def test_personal_project_id_must_resolve_in_rcc_project_scope():
    value=candidates()
    value[0]['personal_project_id']=77
    blocked=build_population_proposal(
        sheet(),[],value,source_evidence(),CONTEXT,
        valid_personal_project_ids=[])
    assert blocked['safe_to_populate'] is False
    assert any(x['code']=='population_personal_project_not_found'
               and x['personal_project_id']==77
               for x in blocked['blockers'])

    allowed=build_population_proposal(
        sheet(),[],value,source_evidence(),CONTEXT,
        valid_personal_project_ids=[77])
    assert allowed['safe_to_populate'] is True
    project=next(x for x in allowed['entities'] if x['entity_type']==PROJECT_TYPE)
    assert project['data']['personal_project_id']==77
