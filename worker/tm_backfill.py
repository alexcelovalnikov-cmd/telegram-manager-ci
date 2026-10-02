"""Bounded history page -> one atomic database commit with an idempotent receipt."""
import asyncio
import hashlib
import json
from tm_time import parse_instant

async def run_page(worker, job, parameters, active_check):
    c=worker.c
    progress=job.get('progress') or {}
    read=int(progress.get('read',0));offset=int(progress.get('offset_id',0))
    remaining=parameters['max_messages']-read
    upper=parameters.get('date_to') or parse_instant(job['created_at'])
    lower=parameters.get('date_from')
    kwargs={'limit':min(50,max(0,remaining)),'offset_id':offset}
    if not offset:kwargs['offset_date']=upper
    active_check(c,parameters['group_key'],parameters['chat_id'])
    if not await worker.live(job):return {'state':'cancelled_or_reclaimed'}
    messages=[] if remaining<=0 else list(await asyncio.wait_for(c.client.get_messages(parameters['chat_id'],**kwargs),timeout=45))
    entries=[];reason='limit' if remaining<=0 else 'page'
    for m in messages:
        if not await worker.live(job):return {'state':'cancelled_or_reclaimed'}
        active_check(c,parameters['group_key'],parameters['chat_id'])
        if not getattr(m,'id',None) or not getattr(m,'date',None):continue
        date=parse_instant(m.date)
        if lower and date<lower:
            reason='lower_bound';break
        if date>=upper:continue
        sender=getattr(m,'sender',None)
        name=' '.join(x for x in (getattr(sender,'first_name',None),getattr(sender,'last_name',None)) if x) or getattr(sender,'title',None)
        user={'sender_id':m.sender_id,'sender_name':name,'username':getattr(sender,'username',None)} if getattr(m,'sender_id',None) else None
        entries.append({'message':c.serialize_message(m,name),'attachment':c.media_meta(m),'user':user})
    if reason=='page':
        if len(messages)<kwargs['limit']:reason='history_end'
        elif read+len(messages)>=parameters['max_messages']:reason='limit'
    cursor=min((m.id for m in messages if getattr(m,'id',None)),default=offset)
    args={'p_job_id':job['id'],'p_token':job['claim_token'],
          'p_expected_read':read,'p_expected_offset':offset,'p_entries':entries,
          'p_scanned':len(messages),'p_cursor':cursor,'p_stop_reason':reason}
    stable={k:v for k,v in args.items() if k!='p_token'}
    args['p_page_key']=hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    # No per-message writes before this RPC. Records/counters/cursor/receipt commit together.
    response=await worker.rpc('tm_commit_history_page_v15',args)
    c.jobs_last={'operation':'backfill','job_id':job['id'],'status':response.get('status'),
                 'progress':response.get('progress'),'stop_reason':response.get('stop_reason')}
    return response
