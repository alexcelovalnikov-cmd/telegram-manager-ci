import pytest
from types import SimpleNamespace
from tm_api.review_ui import show, PreparedAnswer
from tm_api.backend import GuardRejected

Q={'id':'00000000-0000-0000-0000-000000000001','revision':2,'title':'Synthetic 20 000 ₽','group_id':7,'task_id':12,'context':{},'actions':['Уточнить','Отложить','Пропустить']}
class Review:
    config=SimpleNamespace(instance_id='macbook-owner')
    def __init__(self,active=True): self.active=active;self.calls=[]
    async def get_current_review_question(self):
        return {'focus':{'version':8,'review_id':Q['id'] if self.active else None},'question':Q if self.active else None}
    async def get_review_bundle(self,**kwargs): return {'due':[Q]}
    async def group_ids(self): return [7]
    async def guarded_write(self,operation,key,payload):
        self.calls.append((operation,payload));self.active=True

@pytest.mark.asyncio
async def test_existing_focus_is_never_replaced():
    s=Review();r=await show(s,[])
    assert not s.calls and len(r['question']['options'])==3
    for option in r['question']['options']:
        assert option['answer']['review_id']==Q['id']
        assert option['answer']['review_revision']==2
        assert option['answer']['expected_version']==8
    assert r['question']['options'][0]['answer']['action']=='clarify'

@pytest.mark.asyncio
async def test_next_question_uses_existing_guard():
    s=Review(False);r=await show(s,[])
    assert s.calls==[('set_review_focus',{'review_id':Q['id'],'review_revision':2,'expected_version':8})]
    assert r['question']['id']==Q['id']

@pytest.mark.asyncio
async def test_prepared_answer_cannot_change_question():
    s=Review()
    answer={'review_id':Q['id'],'review_revision':1,'expected_version':8,'action':'clarify','user_text':'explicit choice','confirmed':True}
    with pytest.raises(GuardRejected):
        await show(s,[PreparedAnswer(label='Уточнить',answer=answer)])

@pytest.mark.asyncio
async def test_unprepared_business_answer_keeps_focus():
    s=Review();old=Q['actions'];Q['actions']=['Оплачено']
    try:
        r=await show(s,[])
        assert r['question']['options'][0]['answer']['action']=='clarify'
        assert r['question']['options'][0]['needs_dialogue'] is True
        assert not s.calls
    finally:Q['actions']=old
