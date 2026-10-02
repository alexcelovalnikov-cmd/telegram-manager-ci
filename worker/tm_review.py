"""One durable review queue for every group; no duplicate notification engine.

Produces factual snapshots for the existing hourly ChatGPT automation. It does
not execute decisions, mutate financial states or contact Telegram recipients.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

class ReviewError(ValueError):
    pass

KINDS={'ambiguous_match','amount_conflict','payment_uncertain','duplicate_candidate',
       'role_conflict','stale_evidence','historical_only','sync_conflict','technical_error',
       'waiting_condition','meeting_topic','incomplete_project','project_proposal','task_candidate','recurring_payment'}
ACTIONS={
 'default':['Уточнить','Отложить','Пропустить'],
 'payment_uncertain':['Ожидает оплату','Уточнить','Оплата получена','Пропустить'],
 'sync_conflict':['Исправить','Уточнить','Отложить'],
 'technical_error':['Исправить','Уточнить','Отложить','Игнорировать'],
 'project_proposal':['Создать','Обновить существующий','Уточнить','Пропустить'],
 'task_candidate':['Добавить в дела','Уточнить','Пропустить'],
 'meeting_topic':['Обсудили','Перенести','Уточнить','Больше не актуально'],
}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def safe_detail(text):
    """Diagnostics only: never leak credentials, URLs or raw server exceptions."""
    text=str(text or '')
    if re.search(r'(eyJ|Bearer|sb_secret_|password|api[_-]?key|secret|https?://|postgres://)',text,re.I):
        return 'Скрытые технические подробности; требуется защищённая диагностика.'
    return text[:600]

def make_item(key,kind,title,context,group_id=None,task_id=None,evidence=None,reference=None,priority=50):
    if kind not in KINDS or not 1<=len(key)<=200:
        raise ReviewError('invalid_review_item')
    if not isinstance(context,dict): raise ReviewError('invalid_review_context')
    evidence=evidence or []
    state={'kind':kind,'title':title,'context':context,'evidence':evidence,'reference':reference or {}}
    return dict(item_key=key,kind=kind,title=title,context=context,evidence=evidence,
                group_id=group_id,task_id=task_id,reference=reference or {},
                priority=priority,content_fingerprint=fingerprint(state),
                actions=ACTIONS.get(kind,ACTIONS['default']))

def reconcile(previous,item,now):
    """Pure reference semantics used by tests; DB owns concurrency in production."""
    if not previous:
        return dict(item,status='open',revision=1,last_shown_revision=None)
    out=dict(previous)
    if previous['content_fingerprint'] != item['content_fingerprint']:
        out.update(item,status='open',revision=previous['revision']+1,snoozed_until=None)
    elif previous.get('status')=='snoozed' and previous.get('snoozed_until') and previous['snoozed_until']<=now:
        out.update(status='open',snoozed_until=None)
    return out

def visible(item,explicit=False):
    if item.get('status') not in ('open','awaiting_user'):return False
    return explicit or item.get('last_shown_revision')!=item.get('revision')

def collect_items(tasks,proposals,groups,candidates=None,meetings=None,health=None,patterns=None,period=None):
    """Task/proposal-derived issues only; no speculative text mining."""
    allowed={g['id']:g for g in groups if g.get('enabled') and g.get('monitoring_enabled')}
    proposed_tasks={p.get('task_id') for p in proposals if p.get('status')=='pending' and not (p.get('result') or {}).get('review_preparation')}
    out=[]
    for p in proposals:
        if p.get('status')!='pending' or p.get('group_id') not in allowed:continue
        if (p.get('result') or {}).get('review_preparation'):continue
        out.append(make_item('project-proposal:'+str(p['id']),'project_proposal',
          'Проверить изменение проекта',{'patch':p['patch'],'history_only':p.get('history_only',False)},
          p['group_id'],p.get('task_id'),p.get('evidence'),{'table':'tm_project_proposals','id':p['id']},75))
    for t in tasks:
        gid=t.get('context_group_id')
        if gid not in allowed or t.get('project_archived_at') or t.get('status')=='cancelled':continue
        errors={k:safe_detail(t[k]) for k in ('sync_error','project_sync_error','reminder_sync_conflict') if t.get(k)}
        if errors:
            out.append(make_item('task-sync:'+str(t['id']),'sync_conflict',t['title'],{'errors':errors},gid,t['id'],priority=90))
        if t.get('record_kind')=='project' and t.get('id') not in proposed_tasks:
            from tm_finance import amount_breakdown
            money=amount_breakdown(t['title'])
            missing=[]
            umbrella=(t.get('project_data') or {}).get('project_structure')=='umbrella'
            if money['raw'] and not money['complete'] and not umbrella:missing.append('amount_or_components')
            # A date is optional, not a blocking field for creating a project.
            if missing:
                out.append(make_item('project-amount:'+str(t['id']),'incomplete_project',t['title'],{'missing':missing,'amounts':money},gid,t['id'],priority=40))
    for candidate in candidates or []:
        gid=candidate.get('context_group_id')
        if gid not in allowed or candidate.get('review_status')!='pending':continue
        kind='task_candidate'
        context={'candidate_type':candidate.get('candidate_type'),'description':candidate.get('description') or ''}
        out.append(make_item('task-candidate:'+str(candidate['id']),kind,candidate['title'],context,gid,
                            candidate.get('linked_task_id'),reference={'table':'task_candidates','id':candidate['id']},priority=65))
    # An open agenda is not itself an error: show only explicitly requested review.
    for meeting in meetings or []:
        gid=meeting.get('context_group_id')
        if gid not in allowed or meeting.get('status')!='pending' or not meeting.get('review_required'):continue
        out.append(make_item('meeting:'+str(meeting['id']),'meeting_topic',meeting['title'],
            {'person_name':meeting.get('person_name'),'details':meeting.get('details')},gid,meeting.get('linked_task_id'),
            reference={'table':'meeting_topics','id':meeting['id']},priority=60))
    for service in health or []:
        error=service.get('last_error')
        state=service.get('status')
        # Deferred optional models and normal stopped state are not errors by themselves.
        if not error and state not in ('degraded','error','failed'):continue
        if error=='analysis_model_setup_required':continue
        out.append(make_item('service:'+str(service.get('instance_id','unknown'))+':'+str(service['service_name']),'technical_error',
            'Проверить '+service['service_name'],{'status':state,'error':safe_detail(error)},
            reference={'table':'integration_health','instance_id':service.get('instance_id')},priority=95))
    if period:
        from tm_finance import occurrence,FinanceError
        for pattern in patterns or []:
            gid=pattern.get('group_id')
            if not pattern.get('enabled') or pattern.get('frequency')!='monthly' or gid not in allowed:continue
            try:
                proposed=occurrence(pattern,period)
            except (FinanceError,ValueError):
                proposed={'period':period,'reason':'Нужна точная дата выплаты для этого месяца.'}
            out.append(make_item('income:'+pattern['pattern_key']+':'+period,'recurring_payment',
                pattern['label'],proposed,gid,evidence=pattern.get('evidence'),
                reference={'table':'tm_income_patterns','pattern_key':pattern['pattern_key'],'period':period},priority=45))
    return out

def collect_v16(data):
    items=collect_items(data.get('tasks',[]),data.get('proposals',[]),data.get('groups',[]),
                        candidates=data.get('candidates',[]),meetings=data.get('meetings',[]),health=data.get('health',[]),
                        patterns=data.get('patterns',[]),period=data.get('period'))
    allowed={g['id'] for g in data.get('groups',[]) if g.get('enabled') and g.get('monitoring_enabled')}
    for w in data.get('workflows',[]):
        if w.get('group_id') not in allowed or not w.get('enabled') or not w.get('hold_reason'):
            continue
        items.append(make_item('workflow:'+str(w['task_id']),'stale_evidence',
            'Проверить завершение работы',
            {'project_kind':w.get('project_kind'),'event_date':w.get('event_date'),
             'reason':w['hold_reason'],'context_token':w.get('context_token')},
            w['group_id'],w['task_id'],reference={'table':'tm_workflows_v16','task_id':w['task_id']},priority=80))
    seen_estimates=set()
    for estimate in sorted(data.get('estimates',[]),key=lambda x:x.get('created_at') or '',reverse=True):
        if estimate.get('attachment_id') in seen_estimates:continue
        seen_estimates.add(estimate.get('attachment_id'))
        if estimate.get('group_id') not in allowed or not isinstance(estimate.get('parsed'),dict):
            continue
        parsed=estimate['parsed'];current=estimate.get('source_current') is True
        context={'file_name':estimate.get('file_name'),'chat_id':estimate['chat_id'],
                 'message_id':estimate['message_id'],'source_current':current,
                 'content_token':estimate['content_token'],
                 'personal_subtotal_rub':parsed.get('personal_subtotal_rub'),
                 'known_personal_subtotal_rub':parsed.get('known_personal_subtotal_rub'),
                 'source_complete':parsed.get('source_complete'),
                 'match_candidates':parsed.get('match_candidates',[]),
                 'rows':[{k:r.get(k) for k in ('line','label','amount_rub','excluded','warning')} for r in parsed.get('rows',[])[:40]],
                 'rows_truncated':len(parsed.get('rows',[]))>40,
                 'estimated_amounts_only':True,
                 'notice':'Смета требует проверки; не является договором или подтверждением оплаты.'}
        items.append(make_item('estimate:'+str(estimate['attachment_id']),
            'incomplete_project' if current else 'stale_evidence',
            'Проверить смету' if current else 'Источник сметы изменился',context,
            estimate['group_id'],reference={'table':'tm_media_reads_v16','attachment_id':estimate['attachment_id']},priority=60))
    # Salary issues are already guarded by the native engine. Publish only its
    # stable reason, not timestamps or the full diagnostic payload.
    for h in data.get('health',[]):
        for issue in ((h.get('details') or {}).get('salary') or {}).get('items',[]):
            if not issue.get('reason'):
                continue
            ts=[t for t in data.get('tasks',[]) if t['id']==issue.get('task_id') and t.get('context_group_id') in allowed and not t.get('project_archived_at')]
            if not ts:continue
            t=ts[0]
            items.append(make_item('salary-runtime:'+str(t['id']),'incomplete_project',t['title'],
                {'reason':issue['reason']},t['context_group_id'],t['id'],priority=55))
    return items


def retain_unseen_estimates(db,instance_id,items,allowed_groups=None):
    """A bounded server result must not auto-resolve older unseen questions."""
    known={x['item_key'] for x in items};offset=0;retained=0
    while True:
        rows=(db.table('tm_review_items').select('*')
              .eq('reference->>generator','client:'+instance_id)
              .like('item_key','estimate:%').in_('status',['open','awaiting_user','snoozed'])
              .order('item_key').range(offset,offset+199).execute().data or [])
        for r in rows:
            if r['item_key'] in known:continue
            if allowed_groups is not None and r.get('group_id') not in allowed_groups:continue
            fields=('item_key','kind','title','context','evidence','group_id','task_id','reference','priority','content_fingerprint','actions')
            items.append({k:r.get(k) for k in fields});known.add(r['item_key']);retained+=1
        if len(items)>1000:raise ReviewError('review_snapshot_too_large_no_publication')
        if len(rows)<200:return retained
        offset+=200


def refresh(db,instance_id):
    """Server validates scopes and deduplicates stable issue fingerprints."""
    data=db.rpc('tm_review_context_v18',{'p_instance_id':instance_id}).execute().data
    if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ('groups','tasks','proposals','workflows','estimates')):
        raise ReviewError('incomplete_review_snapshot_no_publication')
    items=collect_v16(data)
    from tm_payment_window import forecast
    for task in data.get('tasks',[]):
        if task.get('record_kind')!='project' or task.get('project_archived_at') or task.get('status')=='cancelled':continue
        pd=task.get('project_data') or {}
        decision=forecast(task.get('payment_status'),pd)
        if decision['needs_review']:
            items.append(make_item('payment-window:'+str(task['id']),'waiting_condition',__import__('tm_projects').strip_payment_suffix(task['title']),
              {'question':'Ожидаемый срок оплаты истёк или требует проверки. Есть новый срок?',
               'reason':decision['reason'],'forecast':pd.get('payment_window',pd.get('payment_window_rule'))},
              task['context_group_id'],task['id'],priority=60))
    allowed={g['id'] for g in data['groups'] if g.get('enabled') and g.get('monitoring_enabled')}
    retained=retain_unseen_estimates(db,instance_id,items,allowed) if data.get('estimates_truncated') else 0
    if len(items)>1000:raise ReviewError('review_snapshot_too_large_no_publication')
    result=db.rpc('tm_publish_review_v15',{'p_instance_id':instance_id,'p_items':items}).execute().data
    return dict(result,estimates_truncated=bool(data.get('estimates_truncated')),older_estimate_reviews_preserved=retained)
