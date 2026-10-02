from decimal import Decimal

from tm_api.v75.settlement import build_summary
from tm_api.v75.sheets import plan_rows,MARKER

def row(ident,kind,data,revision=1,status='active'):
    return {'id':ident,'entity_type':kind,'data':data,'revision':revision,'status':status}

def sample_summary():
    p='00000000-0000-4000-8000-000000000001'
    shoot='00000000-0000-4000-8000-000000000011'
    equipment='00000000-0000-4000-8000-000000000012'
    pov='00000000-0000-4000-8000-000000000013'
    logistics='00000000-0000-4000-8000-000000000014'
    rows=[
        row(p,'rcc-settlement-project',{'title':'12 сентября Челябинск','date_iso':'2026-09-12','personal_project_id':77}),
        row(shoot,'rcc-settlement-item',{'parent_id':p,'title':'Съемка','category':'shoot','amount_rub':'20000.00','client_settlement':True,'personal_income':True,'pass_through':False,'order':10}),
        row(equipment,'rcc-settlement-item',{'parent_id':p,'title':'Техника','category':'equipment','amount_rub':'14000.00','client_settlement':True,'personal_income':False,'pass_through':True,'recipient':'Рентал','order':20}),
        row(pov,'rcc-settlement-item',{'parent_id':p,'title':'POV Участник Г','category':'pov','amount_rub':'7000.00','client_settlement':True,'personal_income':False,'pass_through':True,'performer':'Участник Г','order':30}),
        row(logistics,'rcc-settlement-item',{'parent_id':p,'title':'Логистика','category':'logistics','amount_rub':'3000.00','client_settlement':True,'personal_income':True,'pass_through':False,'order':40}),
        row('00000000-0000-4000-8000-000000000101','rcc-settlement-payment',{'parent_id':pov,'amount_rub':'5000.00','payment_type':'Участник В перевод','kind':'received','state':'confirmed','occurred_at':'2026-09-20T12:00:00+05:00'}),
        row('00000000-0000-4000-8000-000000000102','rcc-settlement-payment',{'parent_id':pov,'amount_rub':'2000.00','payment_type':'наличные','kind':'received','state':'confirmed','occurred_at':'2026-09-21T12:00:00+05:00'}),
    ]
    return build_summary(rows)

def test_settlement_and_personal_income_are_independent():
    value=sample_summary()
    project=value['projects'][0]
    assert project['settlement_total_rub']=='44000.00'
    assert project['personal_total_rub']=='23000.00'
    assert project['settlement_received_rub']=='7000.00'
    assert project['settlement_remaining_rub']=='37000.00'
    by_title={item['title']:item for item in project['items']}
    assert by_title['Техника']['personal_income'] is False
    assert by_title['Техника']['recipient']=='Рентал'
    assert by_title['Логистика']['personal_income'] is True
    assert by_title['POV Участник Г']['paid'] is True
    assert by_title['POV Участник Г']['payment_display']=='5 000 ₽ Участник В перевод + 2 000 ₽ наличные'

def test_sheet_plan_preserves_legacy_unbound_debt_and_rebuilds_only_managed_rows():
    summary=sample_summary()
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['Монтаж 6 роликов POV июль','','','FALSE','','6'],
        ['', '1я половина',30000,False,'Участник А наличные',''],
        ['old managed','','99999',False,'','','old-project','','1',MARKER],
        ['Итого:',30000],
    ]
    plan=plan_rows(rows,summary)
    assert plan['legacy_remaining']==Decimal('30000')
    assert plan['legacy_unpaid_excluded']==Decimal('30000')
    assert plan['legacy_unpaid_requires_population']==Decimal('0')
    assert plan['legacy_unpaid_excluded_rows']==[{
        'sheet_row':3,'project_title':'Монтаж 6 роликов POV июль',
        'item_title':'1я половина','amount_rub':'30000'}]
    assert plan['canonical_remaining']==Decimal('37000.00')
    assert plan['total']==Decimal('67000.00')
    assert plan['managed_rows']==[3]
    assert len(plan['a_to_e'])==5
    assert plan['a_to_e'][0][0]=='12 сентября Челябинск'
    assert any(r[1]=='Техника' and r[3] is False for r in plan['a_to_e'])

def test_cutover_dry_run_is_deterministic_and_blocks_exact_unbound_project_duplicate():
    from tm_api.v75.sheets import build_cutover_dry_run
    summary=sample_summary()
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['12 сентября Челябинск','','',False,'',''],
        ['', 'Съемка',20000,False,'',''],
        ['Итого:',20000],
    ]
    context={'workspace_id':'w','spreadsheet_id':'sheet','sheet_name':'Лист1','write_mode':'observe'}
    first=build_cutover_dry_run(rows,summary,context)
    second=build_cutover_dry_run(rows,summary,context)
    assert first==second
    assert first['safe_to_cutover'] is False
    assert first['plan_token']==second['plan_token']
    assert any(b['code']=='unbound_legacy_project_matches_canonical' for b in first['blockers'])
    assert any(op['action']=='insert_canonical' and op['project_id']==summary['projects'][0]['id']
               for op in first['operations'])


def test_bound_rows_keep_existing_user_titles_in_cutover_proposal():
    from tm_api.v75.sheets import build_cutover_dry_run
    summary=sample_summary()
    project=summary['projects'][0]
    equipment=next(item for item in project['items'] if item['title']=='Техника')
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев','','','',''],
        ['Моё название проекта','','',False,'','',project['id'],'',project['revision'],MARKER],
        ['', 'Моя техника',14000,False,'','',project['id'],equipment['id'],equipment['revision'],MARKER],
        ['Итого:',0],
    ]
    dry=build_cutover_dry_run(rows,summary)
    project_op=next(op for op in dry['operations']
                    if op.get('kind')=='project' and op.get('project_id')==project['id'])
    item_op=next(op for op in dry['operations']
                 if op.get('kind')=='item' and op.get('item_id')==equipment['id'])
    assert project_op['action']=='rebuild_bound'
    assert project_op['after']['values'][0]=='Моё название проекта'
    assert item_op['action']=='rebuild_bound'
    assert item_op['after']['values'][1]=='Моя техника'


def test_excluded_pov_project_is_not_projected_but_legacy_row_stays_in_total():
    from tm_api.v75.sheets import plan_rows
    p='00000000-0000-4000-8000-000000000201'
    item='00000000-0000-4000-8000-000000000202'
    summary=build_summary([
        row(p,'rcc-settlement-project',{'title':'Монтаж 6 роликов POV июль'}),
        row(item,'rcc-settlement-item',{'parent_id':p,'title':'1я половина','category':'edit',
            'amount_rub':'15000.00','client_settlement':True,'personal_income':True,'order':10}),
    ])
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['Монтаж 6 роликов POV июль','','',False,'',6],
        ['', '1я половина',15000,False,'Участник А наличные',''],
        ['Итого:',15000],
    ]
    plan=plan_rows(rows,summary)
    assert plan['a_to_e']==[]
    assert plan['canonical_remaining']==Decimal('0')
    assert plan['legacy_remaining']==Decimal('15000')
    assert plan['legacy_unpaid_excluded']==Decimal('15000')
    assert plan['legacy_unpaid_requires_population']==Decimal('0')
    assert plan['legacy_unpaid_excluded_rows'][0]['sheet_row']==3
    assert plan['total']==Decimal('15000')
    assert plan['excluded_canonical'][0]['project_id']==p


def test_settlement_never_infers_receipt_from_item_fields():
    p='00000000-0000-4000-8000-000000000301'
    item='00000000-0000-4000-8000-000000000302'
    summary=build_summary([
        row(p,'rcc-settlement-project',{'title':'Новый проект'}),
        row(item,'rcc-settlement-item',{'parent_id':p,'title':'Съемка','category':'shoot',
            'amount_rub':'7000.00','client_settlement':True,'personal_income':True,
            'payment_type':'Участник В перевод','state':'active'}),
    ])
    work=summary['projects'][0]['items'][0]
    assert work['received_rub']=='0'
    assert work['remaining_rub']=='7000.00'
    assert work['paid'] is False
    assert work['payment_display']==''


def test_duplicate_canonical_ids_fail_closed():
    import pytest
    from tm_api.v75.sheets import plan_rows,SheetSyncError
    summary=sample_summary()
    summary['projects'].append(dict(summary['projects'][0]))
    with pytest.raises(SheetSyncError,match='duplicate_or_missing_canonical_project_id'):
        plan_rows([['Шоу'],['Итого:',0]],summary)


def test_unknown_tm_managed_identity_blocks_cutover_instead_of_silent_delete():
    from tm_api.v75.sheets import plan_rows
    summary=sample_summary()
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['legacy managed','','',False,'','',
         '00000000-0000-4000-8000-000000009999','',1,MARKER],
        ['Итого:',0],
    ]
    plan=plan_rows(rows,summary)
    blocker=next(b for b in plan['blockers']
                 if b['code']=='managed_marker_unknown_canonical_id')
    assert blocker['sheet_row']==2
    assert plan['safe_to_cutover'] is False


def test_nonexcluded_legacy_unpaid_is_reported_as_requires_population():
    from tm_api.v75.sheets import build_cutover_dry_run
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['12 сентября Челябинск','','',False,'',''],
        ['', 'Съемка',20000,False,'Участник А ИП',''],
        ['Монтаж 6 роликов POV июль','','',False,'',6],
        ['', '1я половина',15000,False,'Участник А наличные',''],
        ['Итого:',35000],
    ]
    dry=build_cutover_dry_run(rows,{'projects':[]})
    assert dry['legacy_unpaid_rub']=='35000'
    assert dry['legacy_unpaid_excluded_rub']=='15000'
    assert dry['legacy_unpaid_requires_population_rub']=='20000'
    assert dry['legacy_unpaid_excluded_rows']==[{
        'sheet_row':5,'project_title':'Монтаж 6 роликов POV июль',
        'item_title':'1я половина','amount_rub':'15000'}]
    assert dry['legacy_unpaid_requires_population_rows']==[{
        'sheet_row':3,'project_title':'12 сентября Челябинск',
        'item_title':'Съемка','amount_rub':'20000'}]
    assert dry['safe_to_cutover'] is True


def test_gotovo_boev_legacy_unpaid_is_excluded():
    from tm_api.v75.sheets import build_cutover_dry_run
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['Готово боев','','',False,'',''],
        ['', 'Пакет',5000,False,'',''],
        ['Итого:',5000],
    ]
    dry=build_cutover_dry_run(rows,{'projects':[]})
    assert dry['legacy_unpaid_excluded_rub']=='5000'
    assert dry['legacy_unpaid_requires_population_rub']=='0'


def test_total_row_in_b_c_is_not_counted_as_unpaid_legacy_item():
    rows=[
        ['Шоу','Работы','Сумма','Статус','Тип оплаты','Готово боев'],
        ['Монтаж 6 роликов POV сентябрь','','',False,'',0],
        ['', '1я половина',15000,False,'',''],
        ['', '2я половина',15000,False,'',''],
        ['', 'Итого:',30000],
    ]
    plan=plan_rows(rows,{'projects':[]})
    assert plan['legacy_remaining']==Decimal('30000')
    assert plan['legacy_unpaid_excluded']==Decimal('30000')
    assert plan['legacy_unpaid_requires_population']==Decimal('0')
    assert plan['total']==Decimal('30000')
    assert plan['total_label_column']==1
    assert plan['total_amount_column']==2
    assert plan['projected_rows'][-1][1]=='Итого:'
    assert plan['projected_rows'][-1][2]==30000.0
