"""Typed resumable jobs on a separate queue; V8 workers cannot claim these jobs."""
import asyncio
import time
from datetime import datetime,timezone
from types import SimpleNamespace
from rr_common import INSTANCE_ID,safe_error
from tm_analysis import Analyzer,AnalysisError,settings
from tm_time import parse_instant

class JobError(RuntimeError):pass

def validate_job(job):
    op=job.get('operation');p=job.get('parameters')
    fields={'backfill':{'group_key','chat_id','max_messages','date_from','date_to'},
            'analyze':{'group_key','chat_id','max_messages'},
            'import_projects':{'group_key'},'retry_attachment':{'chat_id','message_id'}}
    if job.get('confirmed') is not True or op not in fields or not isinstance(p,dict) or set(p)-fields[op]:raise JobError('invalid_job')
    if op in ('backfill','analyze','import_projects') and (not isinstance(p.get('group_key'),str) or not p['group_key']):raise JobError('group_required')
    if op in ('backfill','analyze','retry_attachment') and (type(p.get('chat_id')) is not int or not p['chat_id']):raise JobError('chat_required')
    if op=='retry_attachment' and (type(p.get('message_id')) is not int or p['message_id']<=0):raise JobError('message_required')
    q=dict(p)
    if op in ('backfill','analyze'):
        limit=q.get('max_messages',1000 if op=='backfill' else 200)
        if type(limit) is not int or not 1<=limit<=50000:raise JobError('invalid_history_limit')
        q['max_messages']=limit
    for k in ('date_from','date_to'):
        if p.get(k):
            try:d=parse_instant(p[k])
            except (ValueError,TypeError):raise JobError('invalid_'+k)
            if d.tzinfo is None:raise JobError('timezone_required_'+k)
            q[k]=d
    if q.get('date_from') and q.get('date_to') and q['date_from']>=q['date_to']:raise JobError('invalid_history_range')
    return q

def active_group(c,group_key,cid):
    if not c.config or not c.allowed(cid):raise JobError('scope_paused')
    groups=[g for g in c.config.monitored_groups() if g['group_key']==group_key and g['id'] in c.config.by_chat.get(cid,[])]
    if len(groups)!=1:raise JobError('chat_not_member_of_requested_group')
    return groups[0]

class TelegramJobs:
    def __init__(self,c):self.c=c;self.analyzer=Analyzer(c)
    async def rpc(self,n,p):return await self.c.io(lambda:self.c.db.rpc(n,p).execute().data)
    async def live(self,j):
        rows=await self.c.io(lambda:self.c.db.table('tm_jobs_v11').select('status,claim_token').eq('id',j['id']).execute().data)
        return bool(rows and rows[0]['status']=='running' and rows[0]['claim_token']==j['claim_token'])
    async def finish(self,j,status,progress=None,result=None,error=None,delay=0):
        return await self.rpc('tm_finish_job_v11',{'p_id':j['id'],'p_token':j['claim_token'],'p_status':status,'p_progress':progress or j.get('progress') or {},'p_result':result or {},'p_error':error,'p_delay':delay})
    async def tick(self):
        c=self.c
        j=await self.rpc('tm_claim_job_v15',{'p_instance_id':INSTANCE_ID,'p_worker':'telegram'})
        if not j:
            c.jobs_error=None
            return
        c.jobs_last={'id':j['id'],'operation':j.get('operation')}
        c.jobs_error=None
        progress=j.get('progress') or {}
        try:
            p=validate_job(j);op=j['operation']
            if op=='retry_attachment':
                if not c.allowed(p['chat_id']):raise JobError('scope_paused')
                result=await self.rpc('tm_retry_attachment_v10',{'p_chat_id':p['chat_id'],'p_message_id':p['message_id']})
                await self.finish(j,'succeeded' if result.get('queued') else 'paused',result=result);return
            g=active_group(c,p['group_key'],p['chat_id'])
            if op=='backfill':
                from tm_backfill import run_page
                await run_page(self,j,p,active_group)
                c.jobs_error=None
            elif op=='analyze':
                count=int(progress.get('examined',0))
                if count>=p['max_messages']:
                    await self.finish(j,'succeeded',progress);return
                result=await self.analyzer.scan(g,p['chat_id'],before_id=progress.get('before_id'),history_only=True,limit=p['max_messages']-count)
                if result['state'] in ('paused','awaiting_project_import','local_model_deferred'):await self.finish(j,'paused',progress,result);return
                count+=result.get('examined',0);old=result.get('oldest_id')
                progress={'examined':count,'before_id':old,'proposals':int(progress.get('proposals',0))+result.get('proposals',0)}
                await self.finish(j,'succeeded' if old is None or count>=p['max_messages'] else 'pending',progress,result,delay=3)
            else:raise JobError('wrong_worker')
        except asyncio.CancelledError:raise
        except Exception as e:
            code=str(e) if isinstance(e,(JobError,AnalysisError)) else safe_error(e)
            wait=getattr(e,'seconds',None)
            if isinstance(wait,int):await self.finish(j,'pending',progress,error='telegram_flood_wait',delay=min(86400,wait+5))
            else:await self.finish(j,'paused' if code in ('scope_paused','analysis_model_setup_required') else 'failed',progress,error=code[:150])
            c.jobs_error=code[:150]

async def jobs_loop(c):
    worker=TelegramJobs(c)
    while not c.stop.is_set():
        try:await worker.tick()
        except asyncio.CancelledError:raise
        except Exception as e:c.jobs_error=safe_error(e)
        try:await asyncio.wait_for(c.stop.wait(),timeout=5)
        except asyncio.TimeoutError:pass

async def analysis_loop(c):
    analyzer=Analyzer(c)
    while not c.stop.is_set():
        pause=90
        try:
            st=settings(c.analysis_settings);pause=st['interval']
            if st['mode'] != 'local_model':
                c.analysis_last={'state':st['mode'],'local_model_called':False}
                c.analysis_error=None
            elif st['enabled'] and c.config:
                for cid,gids in list(c.config.by_chat.items()):
                    if c.stop.is_set():break
                    if not c.allowed(cid) or len(gids)!=1:continue
                    g=c.config.groups[next(iter(gids))]
                    result=await analyzer.scan(g,cid)
                    c.analysis_last=result
                c.analysis_error=None
        except asyncio.CancelledError:raise
        except Exception as e:c.analysis_error=str(e)[:150] if isinstance(e,AnalysisError) else safe_error(e)
        try:await asyncio.wait_for(c.stop.wait(),timeout=pause)
        except asyncio.TimeoutError:pass
