"""Pure release/wiring checks. Not database or CalDAV integration tests."""
from pathlib import Path
import ast
from tm_api.v24.engine import Engine
from tm_api.v25.configuration import Workspace
from tm_api.v25.payments import FinanceBusiness
from tm_api.v24.business import Business
from tm_api.v25.business import ConfigBusiness
from tm_api.v25 import finance
from tm_api.v24.common import Rejected
import pytest
R=Path(__file__).resolve().parents[2]


def test_image_contains_imported_server_reminders_package():
    for name in ('Dockerfile','deploy/v25/Dockerfile.calendar'):
        assert 'COPY tm_reminders ./tm_reminders' in (R/name).read_text()


def test_payment_correction_uses_ledger_v25_only_when_enabled():
    m={'operation':'update_payment'}
    assert isinstance(Engine(None,'test',configurable=True).planner(m),FinanceBusiness)
    assert isinstance(Engine(None,'test',configurable=False).planner(m),Business)


def test_stage_is_separate_and_not_default_production():
    source=(R/'deploy/v25/compose.stage.yaml').read_text()
    assert 'name: telegram-manager-v25-stage' in source
    assert 'internal: true' in source
    assert '/opt/telegram-manager/database' not in source
    assert 'services: {}' in (R/'deploy/compose.yaml').read_text()
    assert 'TM_CONFIGURABLE_ENABLED=true' in (R/'deploy/v25/create_stage_config.py').read_text()


def test_no_config_rules_run_as_python():
    for p in (R/'tm_api/v25').glob('*.py'):
        tree=ast.parse(p.read_bytes())
        forbidden=[n.func.id for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('eval','exec','compile')]
        assert not forbidden,p


def test_new_list_reassignment_has_distinct_stable_identity(monkeypatch):
    from tm_calendar import repository as calendar
    monkeypatch.setattr(calendar,'api_user',lambda tx,instance:'synthetic-owner')
    class ExistsOnly:
        def one(self,query,params=()):return None
    business=ConfigBusiness('synthetic');tx=ExistsOnly();body={'mode':'server_new','title':'List'}
    first=business._destination(tx,body,'same-workspace',1)
    second=business._destination(tx,body,'same-workspace',2)
    assert first['id']!=second['id']
    assert second==business._destination(tx,body,'same-workspace',2)


def test_cost_receipts_unknown_tax_not_deduced():
    value=finance.cost({'work_amount':'20000','document_total':'21200'})
    assert value['tax_amount'] is None
    assert value['work_amount']=='20000'


def test_example_is_configuration_not_executable_workflow():
    import json
    example=json.loads((R/'examples/v25/postproduction.json').read_text())
    value=Workspace.model_validate(example['mutation']['changes'])
    assert len(value.chat_ids)==5 and value.reminder_list.mode=='server_new'
    assert value.default_tags==['постпродакшен']
    rules=[d for d in value.documents if d.kind=='rule']
    assert len(rules)>=3


def test_schema_is_available_without_db_or_owner_account():
    from tm_api.v25.reading import ConfigReader
    result=ConfigReader(None,'synthetic').read('schema')
    assert result['workspace']['properties']['name']
    assert result['semantics']['workspace_is_not_billing_project'] is True
    assert result['semantics']['legacy_list_migration_supported'] is True
    assert result['semantics']['workspace_storage_migration_operation']=='migrate_workspace_storage'
