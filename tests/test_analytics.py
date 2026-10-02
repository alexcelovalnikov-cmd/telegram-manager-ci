from copy import deepcopy
import pytest
from pydantic import ValidationError
from tm_api.analytics import FinancialQuery, summarize


def project(i, **data):
    return {'id':i,'title':'10.20 Project - 40к ⚡️','group_key':'work','status':'open','payment_status':'awaiting',
            'project_data':{'amount_rub':'40000','date_iso':'2026-10-20','project_kind':'shoot',**data},
            'ledger':{'received_net':'0.00','confirmed_component_sum':'0.00','scope_complete':False}}


def test_requests_rejections_salary_and_legacy_markers_excluded():
    rows=[project(1),project(2,request_state='pending'),project(3,request_state='rejected'),project(4,salary=True),project(5,request_state='confirmed'),project(6)]
    rows[-1]['title']='➕ Potential - 40к'
    before=deepcopy(rows)
    r=summarize({'items':rows},FinancialQuery())
    assert r['project_count']==2 and r['totals']['recorded_amount']=='80000.00'
    assert {e['id'] for e in r['excluded']}=={2,3,4,6} and rows==before
    assert r['totals']['received_net']=='0.00'


def test_unknown_year_not_inferred_and_archives_preserved():
    old=project(1);old.update(status='completed',project_archived_at='2026-10-31T00:00:00Z',payment_status='paid')
    unknown=project(2,date_iso=None,date_mmdd='10.20')
    r=summarize({'items':[old,unknown,project(3,date_iso='2025-10-20')]},FinancialQuery(from_date='2026-10-01',to_date='2026-10-31'))
    assert [p['id'] for p in r['projects']]==[1]
    assert r['excluded']==[{'id':2,'reason':'project_year_or_date_unknown'}]
    assert r['payment_status_counts']=={'paid':1} and r['totals']['received_net']=='0.00'


def test_estimates_and_incomplete_amounts_never_become_totals_or_receipts():
    rows=[project(1,amount_breakdown={'complete':False,'total':None,'known_subtotal':'15000','proposed_total':'20000'}),
          project(2,amount_rub='10000',amount_status='estimate'),
          project(3,amount_breakdown={'complete':True,'total':'40000.01'})]
    rows[2]['ledger']['received_net']='10000.01'
    r=summarize({'items':rows},FinancialQuery())
    assert r['totals']=={'recorded_amount':'40000.01','confirmed_components':'0.00','received_net':'10000.01','awaiting_recorded_amount':'40000.01'}
    assert r['incomplete_amount_count']==2


def test_filters_reject_bad_dates_and_do_not_double_count_work_types():
    for p in [{'from_date':'2026-02-30'},{'from_date':'2026-10-02','to_date':'2026-10-01'},{'from_date':'2026-1-1'},{'sql':'select 1'}]:
        with pytest.raises(ValidationError):FinancialQuery(**p)
    r=summarize({'items':[project(1),project(2,project_kind='edit')]},FinancialQuery(work_type='shoot',group_key='work'))
    assert [p['id'] for p in r['projects']]==[1]


def test_mixed_project_has_no_duplicate_amount_or_guessed_payment_allocation():
    p=project(1,project_kind='mixed')
    p['workstreams']=[{'work_type':'shoot','amount':'10000.00'},{'work_type':'edit','amount':'30000.00'}]
    r=summarize({'items':[p]},FinancialQuery(work_type='edit'))
    assert r['project_count']==1 and r['totals']['recorded_amount']=='40000.00'
    assert r['work_type_amounts']=={'edit':{'recorded_amount':'30000.00','unknown_components':0}}
    assert r['totals']['received_net']=='0.00'
