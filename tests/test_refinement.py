from types import SimpleNamespace
import pytest
from tm_api.refinement import prepare
from tm_api.write_models import ReviewProjectChange
from tm_api.project_model.tm_finance import amount_breakdown
from tm_api.project_model.tm_projects import parse_title,render_title
from tm_api.review_ui import show
from test_review_ui import Review,Q
from pydantic import ValidationError

BASE={'review_id':Q['id'],'review_revision':2,'expected_version':8,'project_id':12,
'expected_updated_at':'2026-09-28T00:00:00Z','patch':{'amount_expression':'15к + CC 5k'},
'evidence':[{'kind':'telegram','chat_id':-42,'message_id':3,'content_token':'current'}],
'evidence_assessment':'supported','rationale':'The question and reply establish the additional component.'}
class Prepare:
    def __init__(self):self.calls=[]
    async def get_review_sources(self,*args):return {}
    async def guarded_write(self,op,key,payload):self.calls.append((op,key,payload));return {'prepared':True}

@pytest.mark.asyncio
async def test_generic_components_and_preparation_not_confirmation():
    s=Prepare();await prepare(s,'prepare-key-000001',ReviewProjectChange(**BASE))
    op,key,p=s.calls[0];assert op=='prepare_review_project_change' and 'confirmed' not in p
    assert p['patch']['amount_rub']=='20000.00' and p['patch']['amount_breakdown']['terms'][1]['label']=='CC'
    assert amount_breakdown('Project - съёмка 10к + монтаж 4к')['total']=='14000.00'
    title='09.16 Демо Клиент - 15к + CC 5k'; parsed=parse_title(title)
    assert parsed['label']=='Демо Клиент' and parsed['amount_rub']=='20000.00'
    assert render_title(parsed)==title
    assert parsed['amount_breakdown']['confirmation']=='imported_unverified'

@pytest.mark.asyncio
async def test_ambiguous_creates_no_proposal():
    for change in [BASE|{'evidence_assessment':'ambiguous'},BASE|{'patch':{'amount_expression':'15к + CC 5k?'}}]:
        s=Prepare();r=await prepare(s,'ambiguous-key-0001',ReviewProjectChange(**change))
        assert not r['prepared'] and r['needs_clarification'] and not s.calls

@pytest.mark.parametrize('patch',[{}, {'payment_status':'paid'},{'work_status':'delivered'},{'amount_expression':'5к','amount_rub':'5000'},{'label':None}])
def test_no_payment_salary_or_empty_patch(patch):
    with pytest.raises(ValidationError):ReviewProjectChange(**(BASE|{'patch':patch}))

@pytest.mark.asyncio
async def test_concrete_button_one_existing_atomic_answer():
    s=Review();read=s.get_current_review_question
    async def current():
        r=await read();r['project_change']={'prepared':True,'before_title':'09.16 Демо Клиент - 15к + ?',
        'after_title':'09.16 Демо Клиент - 15к + CC 5k','rationale':'Evidence supports component.',
        'apply_arguments':{'project_id':12,'expected_updated_at':BASE['expected_updated_at'],'proposal_id':'00000000-0000-0000-0000-000000000042','expected_proposal_updated_at':BASE['expected_updated_at'],'evidence':BASE['evidence'],'confirmed':True}}
        return r
    s.get_current_review_question=current
    r=await show(s,[]);o=r['question']['options'][0]
    assert o['label']=='Обновить: 09.16 Демо Клиент - 15к + CC 5k'
    assert o['answer']['action']=='resolved' and o['answer']['effect']['operation']=='update_existing_project'
    assert o['answer']['expected_version']==8 and r['selected_button_style']=='solid_blue'
    assert not s.calls
