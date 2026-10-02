import pytest
from tm_api.jobs import JobStore, Worker
from tm_api.security import Audit

class Service:
    offline = False
    mac = True
    calls = []
    sheet_calls = 0
    async def get_system_status(self):
        if self.offline: raise RuntimeError('sensitive upstream error')
        return {'mac_available':self.mac}
    async def get_review_bundle(self, **kwargs):
        self.calls.append(kwargs)
        if self.offline: raise RuntimeError('sensitive upstream error')
        return {'due':[]}
    async def sync_rcc_sheet(self):
        self.sheet_calls += 1
        if self.offline: raise RuntimeError('sensitive upstream error')
        return {'status':'observe'}

@pytest.mark.asyncio
async def test_bounded_restart_outage_recovery_and_no_business_writes(tmp_path):
    path=tmp_path/'jobs.sqlite'
    store=JobStore(path);audit=Audit(tmp_path/'audit.sqlite');service=Service();worker=Worker(store,service,audit)
    service.offline=True
    await worker.once(1);await worker.once(1);await worker.once(1)
    assert all(j['outcome']=='unavailable' for j in store.status()['jobs'])
    assert len(store.status()['jobs'])==3
    store.close();store=JobStore(path);worker=Worker(store,service,audit)
    assert await worker.once(2) is False
    service.offline=False;service.mac=False
    await worker.once(32);await worker.once(32);await worker.once(32)
    assert {j['outcome'] for j in store.status()['jobs']}=={'runtime_offline','ok','observe'}
    assert all(c=={'explicit':False,'limit':20} for c in service.calls)
    rows=audit.db.execute('SELECT operation,outcome FROM events').fetchall()
    assert ('job_review_preparation','recovered') in rows
    assert 'sensitive' not in str(rows)
    # Crash after claiming: retry on restart, without creating another queue item.
    store.claim(100);store.close();store=JobStore(path)
    assert store.claim(101) is not None and len(store.status()['jobs'])==3
    store.close();audit.db.close()
