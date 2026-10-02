import pytest
from pydantic import ValidationError

from tm_api.v24.common import Rejected
from tm_api.v24.engine import Engine,RECONCILIATION_AUTO_OPS
from tm_api.v25.configuration import Workspace

RULE_ID='00000000-0000-4000-8000-000000000777'
WORKSPACE_ID='00000000-0000-4000-8000-000000000001'

def test_rcc_settlement_workspace_accepts_exact_google_sheet_integration():
    value=Workspace(
        key='rcc',name='Проекты и оплаты',
        analysis={'profile':'rcc_settlement'},
        integrations={'google_sheets':{
            'enabled':True,'mode':'rcc_settlement',
            'spreadsheet_id':'18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk',
            'sheet_name':'Лист1','write_mode':'observe'}})
    assert value.analysis['profile']=='rcc_settlement'
    assert value.integrations['google_sheets']['write_mode']=='observe'

@pytest.mark.parametrize('integration',[
    {'google_sheets':{'mode':'rcc_settlement','spreadsheet_id':'short'}},
    {'google_sheets':{'mode':'other','spreadsheet_id':'18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk'}},
    {'google_sheets':{'mode':'rcc_settlement','spreadsheet_id':'18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk','write_mode':'overwrite_all'}},
    {'unknown':{}},
])
def test_google_sheet_integration_is_closed_data(integration):
    with pytest.raises((ValidationError,Rejected)):
        Workspace(key='rcc',name='RCC',integrations=integration)

class GuardTx:
    def one(self,sql,args=()):
        if 'FROM tm_config.workspaces' in sql:
            return {'settings':{'analysis':{'profile':'rcc_settlement'}}}
        if 'FROM tm_config.documents' in sql:
            return {'id':RULE_ID,'revision':3}
        if 'FROM tm_config.entities' in sql:
            return {'entity_type':'rcc-settlement-payment'}
        raise AssertionError(sql)

def engine():
    e=Engine.__new__(Engine)
    e.instance='macbook-owner'
    return e

def test_rcc_project_and_item_entities_can_use_bound_reconciliation_rule():
    mutation={'operation':'create_managed_entity','workspace_id':WORKSPACE_ID,
        'changes':{'entity_type':'rcc-settlement-item','data':{}},
        'analysis':{'rule_refs':[{'id':RULE_ID,'revision':3}]}}
    engine()._validate_managed_reconciliation_rule(GuardTx(),mutation,'rcc.settlement.item')
    assert 'move_calendar_event' in RECONCILIATION_AUTO_OPS

def test_rcc_payment_entity_never_uses_standing_reconciliation_authority():
    mutation={'operation':'create_managed_entity','workspace_id':WORKSPACE_ID,
        'changes':{'entity_type':'rcc-settlement-payment','data':{}},
        'analysis':{'rule_refs':[{'id':RULE_ID,'revision':3}]}}
    with pytest.raises(Rejected,match='payment_requires_user_confirmation'):
        engine()._validate_managed_reconciliation_rule(GuardTx(),mutation,'rcc.settlement.payment')
