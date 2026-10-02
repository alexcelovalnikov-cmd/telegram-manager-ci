from dataclasses import replace
import pytest
from starlette.testclient import TestClient
from test_gateway import Fake, setup, HEADERS
from tm_api.app import create_app
from tm_api.backend import GuardRejected
from tm_api.service import Service

class Sources(Fake):
    error = None
    async def rpc(self, name, args):
        if name.endswith('_v21'):
            self.calls.append((name,args))
            return {'error':self.error} if self.error else {'messages':[], 'sources':{'chats':[]}}
        return await super().rpc(name,args)

@pytest.mark.parametrize('body',[
    {'project_id':1,'chat_id':-1,'instance_id':'another'},
    {'project_id':1,'chat_id':-1,'limit':31},
    {'project_id':1,'chat_id':-1,'query':'x'*201},
    {'project_id':1,'chat_id':'-1'},
    {'project_id':1,'chat_id':-1,'before_message_id':0},
])
def test_source_validation_before_db(setup,body):
    config,_=setup; db=Sources()
    with TestClient(create_app(config,db)) as client:
        assert client.post('/api/search_project_messages',json=body,headers=HEADERS).status_code==422
    assert not db.calls

def test_source_routes_fixed_owner_and_read_only(setup):
    config,_=setup; db=Sources()
    with TestClient(create_app(config,db)) as c:
        assert c.post('/api/search_project_messages',json={'project_id':1,'chat_id':-10,'query':"50%_'"},headers=HEADERS).status_code==200
        name,args=db.calls[-1]
        assert name=='tm_search_project_messages_v21' and args['p_instance_id']==config.instance_id
        assert args['p_query']=="50%_'" and 'p_before_message_id' not in args
        db.error='not_found'
        assert c.post('/api/get_project_sources',json={'project_id':99},headers=HEADERS).status_code==404
        db.error='guard_rejected'
        r=c.post('/api/get_review_sources',json={'review_id':'00000000-0000-0000-0000-000000000001','review_revision':2,'expected_version':1},headers=HEADERS)
        assert r.status_code==409 and not r.json()['retryable']

@pytest.mark.asyncio
async def test_current_question_hydrates_sources_and_rejects_race(setup):
    config,_=setup
    class Active(Sources):
        async def rpc(self,name,args):
            if name=='tm_review_focus_get_v18':
                return {'current':True,'review_id':'00000000-0000-0000-0000-000000000001','review_revision':2,'version':8}
            return await super().rpc(name,args)
        async def rows(self,table,**query):
            if table=='tm_review_items':
                return [{'id':'00000000-0000-0000-0000-000000000001','revision':2,'group_id':7,'task_id':1}]
            return await super().rows(table,**query)
    db=Active(); s=Service(db,config)
    result=await s.get_current_review_question()
    assert result['source_context']['sources']=={'chats':[]}
    assert db.calls[-1][1]['p_expected_version']==8
    db.error='guard_rejected'
    with pytest.raises(GuardRejected): await s.get_current_review_question()
