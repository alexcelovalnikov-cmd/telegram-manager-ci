import pytest

from tm_api.v75 import sheets
from tm_api.v75.settlement import build_summary


WORKSPACE_ID='00000000-0000-4000-8000-000000000001'
SPREADSHEET_ID='18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk'


def row(ident,kind,data,revision=1,status='active'):
    return {'id':ident,'entity_type':kind,'data':data,'revision':revision,'status':status}


def sample_summary():
    project='00000000-0000-4000-8000-000000000001'
    item='00000000-0000-4000-8000-000000000011'
    payment='00000000-0000-4000-8000-000000000101'
    return build_summary([
        row(project,'rcc-settlement-project',{'title':'12 сентября Челябинск','date_iso':'2026-09-12'}),
        row(item,'rcc-settlement-item',{'parent_id':project,'title':'Съемка','category':'shoot',
            'amount_rub':'20000.00','client_settlement':True,'personal_income':True,'order':10}),
        row(payment,'rcc-settlement-payment',{'parent_id':item,'amount_rub':'20000.00',
            'payment_type':'Участник А ИП','kind':'received','state':'confirmed',
            'occurred_at':'2026-09-20T12:00:00+05:00'}),
    ])


class FakeGoogleSheets:
    instances=[]
    def __init__(self,_path):
        self.structural_calls=[]
        self.write_calls=[]
        self.__class__.instances.append(self)
    def metadata(self,_spreadsheet_id):
        return {'sheets':[{'properties':{'sheetId':7,'title':'Лист1'}}]}
    def values(self,_spreadsheet_id,_sheet_name):
        return [
            ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
            ['12 сентября Челябинск','','',False,'',''],
            ['', 'Съемка',20000,False,'',''],
            ['Итого:',20000],
        ]
    def structural(self,*args):
        self.structural_calls.append(args)
    def write_values(self,*args):
        self.write_calls.append(args)


def configured(write_mode='observe'):
    summary=sample_summary()
    summary['workspace_id']=WORKSPACE_ID
    summary['workspace_key']='rcc'
    return {'workspace':{'id':WORKSPACE_ID},'summary':summary,'integration':{
        'enabled':True,'mode':'rcc_settlement','spreadsheet_id':SPREADSHEET_ID,
        'sheet_name':'Лист1','write_mode':write_mode}}


def test_explicit_dry_run_never_writes_even_if_configuration_is_managed(monkeypatch):
    FakeGoogleSheets.instances.clear()
    monkeypatch.setattr(sheets,'GoogleSheets',FakeGoogleSheets)
    monkeypatch.setattr(sheets,'_load',lambda *_args:configured('managed'))
    result=sheets.dry_run_configured(None,'instance','/credentials.json',WORKSPACE_ID)
    client=FakeGoogleSheets.instances[-1]
    assert result['status']=='observe'
    assert result['mode']=='dry_run'
    assert result['write_mode']=='managed'
    assert result['safe_to_cutover'] is False
    assert client.structural_calls==[]
    assert client.write_calls==[]


def test_managed_sync_fails_before_sheet_writes_when_cutover_has_blockers(monkeypatch):
    FakeGoogleSheets.instances.clear()
    monkeypatch.setattr(sheets,'GoogleSheets',FakeGoogleSheets)
    monkeypatch.setattr(sheets,'_load',lambda *_args:configured('managed'))
    with pytest.raises(sheets.SheetSyncError,match='rcc_sheet_cutover_blocked'):
        sheets.sync_configured(None,'instance','/credentials.json')
    client=FakeGoogleSheets.instances[-1]
    assert client.structural_calls==[]
    assert client.write_calls==[]


def test_dry_run_reports_excluded_legacy_debt_without_requiring_population(monkeypatch):
    class ExcludedOnlyGoogleSheets(FakeGoogleSheets):
        def values(self,_spreadsheet_id,_sheet_name):
            return [
                ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
                ['Монтаж 6 роликов POV июль','','',False,'',6],
                ['', '1я половина',15000,False,'Участник А наличные',''],
                ['', '2я половина',15000,False,'Участник Б ИП',''],
                ['Итого:',30000],
            ]
    empty=build_summary([])
    empty['workspace_id']=WORKSPACE_ID
    empty['workspace_key']='rcc'
    cfg={'workspace':{'id':WORKSPACE_ID},'summary':empty,'integration':{
        'enabled':True,'mode':'rcc_settlement','spreadsheet_id':SPREADSHEET_ID,
        'sheet_name':'Лист1','write_mode':'observe'}}
    monkeypatch.setattr(sheets,'GoogleSheets',ExcludedOnlyGoogleSheets)
    monkeypatch.setattr(sheets,'_load',lambda *_args:cfg)
    result=sheets.dry_run_configured(None,'instance','/credentials.json',WORKSPACE_ID)
    assert result['legacy_unpaid_rub']=='30000'
    assert result['legacy_unpaid_excluded_rub']=='30000'
    assert result['legacy_unpaid_requires_population_rub']=='0'
    assert [row['sheet_row'] for row in result['legacy_unpaid_excluded_rows']]==[3,4]
    assert result['legacy_unpaid_requires_population_rows']==[]
    assert result['safe_to_cutover'] is True


def test_managed_sync_preserves_b_c_total_layout_and_styles(monkeypatch):
    project='00000000-0000-4000-8000-000000000001'
    item='00000000-0000-4000-8000-000000000011'
    summary=build_summary([
        row(project,'rcc-settlement-project',{'title':'17 октября RCC Karate Kombat'}),
        row(item,'rcc-settlement-item',{'parent_id':project,'title':'Съемка','category':'shoot',
            'amount_rub':'20000.00','client_settlement':True,'personal_income':True,'order':10}),
    ])
    summary['workspace_id']=WORKSPACE_ID
    summary['workspace_key']='rcc'

    class BoundBCTotalGoogleSheets(FakeGoogleSheets):
        def values(self,_spreadsheet_id,_sheet_name):
            return [
                ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев',
                 'TM Project ID','TM Item ID','TM Revision','TM Managed'],
                ['Монтаж 6 роликов POV сентябрь','','',False,'',0],
                ['', '1я половина',15000,False,'',''],
                ['', '2я половина',15000,False,'',''],
                ['17 октября RCC Karate Kombat','','',False,'','',project,'',1,sheets.MARKER],
                ['', 'Съемка',20000,False,'','',project,item,1,sheets.MARKER],
                ['', 'Итого:',50000],
            ]

    cfg={'workspace':{'id':WORKSPACE_ID},'summary':summary,'integration':{
        'enabled':True,'mode':'rcc_settlement','spreadsheet_id':SPREADSHEET_ID,
        'sheet_name':'Лист1','write_mode':'managed'}}
    BoundBCTotalGoogleSheets.instances.clear()
    monkeypatch.setattr(sheets,'GoogleSheets',BoundBCTotalGoogleSheets)
    monkeypatch.setattr(sheets,'_load',lambda *_args:cfg)

    result=sheets.sync_configured(None,'instance','/credentials.json')
    client=BoundBCTotalGoogleSheets.instances[-1]
    assert result['status']=='ok'
    assert result['legacy_unpaid_rub']=='30000'
    assert result['canonical_unpaid_rub']=='20000.00'
    assert result['total_unpaid_rub']=='50000.00'
    assert client.write_calls
    data=client.write_calls[-1][1]
    assert any(d['range']=="'Лист1'!C7" and d['values']==[[50000.0]] for d in data)
    requests=client.structural_calls[-1][1]
    assert not any('setDataValidation' in r for r in requests)
    project_styles=[r for r in requests if 'repeatCell' in r and
        r['repeatCell']['range'].get('startRowIndex')==4 and
        r['repeatCell']['range'].get('endColumnIndex')==6]
    assert project_styles
