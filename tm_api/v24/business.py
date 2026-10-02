"""Business plans reuse V22's title/evidence/payment rules, not review workflows."""
import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .common import Rejected, check_revision, encoded, strict_fields
from . import scope
from ..project_model.tm_finance import amount_breakdown
from ..project_model.tm_projects import possible_duplicates, render_title, amount

TASK_COLUMNS=('title','description','status','due_at','reminder_due_spec','tags')
PROJECT_COLUMNS=('title','project_data','status','payment_status','payment_evidence','payment_confirmed_at','project_archived_at')


def _text(value, limit, empty=True):
    if not isinstance(value,str) or len(value)>limit or '\x00' in value or (not empty and not value.strip()):
        raise Rejected('invalid_text')
    return value


def _money(value):
    if not isinstance(value,str): raise Rejected('money_requires_decimal_string')
    try: return amount(value)
    except (RuntimeError,ValueError,InvalidOperation): raise Rejected('invalid_amount') from None


def task_state(row):
    return {k:row.get(k) for k in ('id','record_kind','context_group_id','updated_at',*TASK_COLUMNS,'target_app','project_data','payment_status','payment_evidence','payment_confirmed_at','project_archived_at')}

class Business:
    def __init__(self, instance):
        self.instance=instance

    def plan(self, tx, m, lock=False):
        op=m['operation'];changes=m['changes']
        if op=='update_payment': return self.payment_plan(tx,m,lock)
        creating=op in ('create_task','create_project')
        if creating:
            if not m.get('group_key'): raise Rejected('group_required')
            g=scope.group(tx,self.instance,m['group_key'],lock)
            if lock:
                tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('project_import:'+self.instance+':'+str(g.get('reminder_list_id')),))
            original=None
            row={'id':None,'record_kind':'project' if op=='create_project' else 'task','context_group_id':g['id'],
                 'title':'','description':'','status':'open','due_at':None,'reminder_due_spec':{'kind':'none'},
                 'tags':[],'target_app':None if m.get('_server_managed') else 'reminders','project_data':{},'payment_status':'unknown',
                 'payment_evidence':None,'payment_confirmed_at':None,'project_archived_at':None,'updated_at':None}
        else:
            original=scope.task(tx,self.instance,m['target_id'],lock)
            row=copy.deepcopy(original)
            g=tx.one('SELECT * FROM public.telegram_chat_groups WHERE id=%s'+(' FOR SHARE' if lock else ''),(row['context_group_id'],))
            check_revision(row['updated_at'],m['expected_revision'])
            if m.get('group_key') not in (None,g['group_key']): raise Rejected('group_mismatch')
            if row.get('project_archived_at') and op!='archive_project': raise Rejected('archived_project_requires_explicit_restore')
            scope.no_salary(tx,row['id'])
        scope.evidence(tx,self.instance,m['evidence'],g['id'],lock)
        before=task_state(original) if original else None
        if op in ('create_task','update_task','complete_task'):
            if row['record_kind']!='task': raise Rejected('use_project_operation')
            if row.get('target_app') not in (None,'reminders'): raise Rejected('legacy_calendar_task_requires_migration')
            strict_fields(changes,() if op=='complete_task' else TASK_COLUMNS, ('title',) if creating else ())
            if op=='complete_task': row['status']='completed'
            else: row.update(changes)
            _text(row['title'],500,False); _text(row.get('description') or '',16000)
            if row['status'] not in ('open','waiting','completed','cancelled'): raise Rejected('invalid_task_status')
            if not isinstance(row['tags'],list) or len(row['tags'])>30 or any(not isinstance(x,str) or len(x)>100 for x in row['tags']): raise Rejected('invalid_tags')
            if changes.get('reminder_due_spec')=={'kind':'none'} and changes.get('due_at') is not None:
                raise Rejected('conflicting_due_spec')
            if 'reminder_due_spec' in changes and changes['reminder_due_spec'] not in (None,{'kind':'none'}):
                raise Rejected('use_due_at_or_preserve_existing_due_spec')
            if changes.get('reminder_due_spec')=={'kind':'none'}:
                row['due_at']=None
            if 'due_at' in changes:
                if changes['due_at']:
                    d=datetime.fromisoformat(changes['due_at'].replace('Z','+00:00'))
                    if d.tzinfo is None: raise Rejected('timezone_required')
                # Existing sync infers a fresh due spec from this legacy field.
                if 'reminder_due_spec' not in changes: row['reminder_due_spec']=None
            if creating and tx.one('SELECT id FROM public.tasks WHERE context_group_id=%s AND record_kind=\'task\' AND lower(btrim(title))=lower(btrim(%s)) AND status IN (\'open\',\'waiting\') LIMIT 1',(g['id'],row['title'])):
                raise Rejected('possible_duplicate_task')
        elif op in ('create_project','update_project','archive_project','set_project_status','set_payment_state'):
            if row['record_kind']!='project' or (g['rules_profile']!='projects_payments' and not m.get('_server_managed')): raise Rejected('use_task_operation')
            data=copy.deepcopy(row.get('project_data') or {})
            if op=='archive_project':
                if row.get('project_archived_at'):
                    raise Rejected('project_already_archived')
                strict_fields(changes,('reason',),('reason',))
                reason=_text(changes['reason'],1000,False)
                if row.get('payment_status') in ('partial','paid') or tx.one(
                    "SELECT 1 AS found FROM public.tm_payment_allocations a JOIN public.tm_payment_events p ON p.id=a.payment_id "
                    "WHERE a.task_id=%s AND p.state='confirmed' LIMIT 1",(row['id'],)):
                    raise Rejected('financial_project_with_receipts_cannot_archive')
                data['archive_reason']=reason
                row['project_data']=data
                row['project_archived_at']='assigned_at_commit'
            elif op in ('create_project','update_project'):
                strict_fields(changes,('label','date_iso','date_mmdd','amount_rub','amount_expression','aliases','description'),('label',) if creating else ())
                if 'amount_rub' in changes and 'amount_expression' in changes: raise Rejected('choose_total_or_expression')
                for key in ('label','date_iso','date_mmdd','aliases'):
                    if key in changes: data[key]=changes[key]
                if 'description' in changes:
                    # V18 prohibits notes in native project Reminders. Keep manual notes intact.
                    data['server_description']=_text(changes['description'],16000)
                if 'aliases' in changes:
                    if not isinstance(changes['aliases'],list) or len(changes['aliases'])>30: raise Rejected('invalid_aliases')
                    for alias in changes['aliases']: _text(alias,200,False)
                if 'date_iso' in changes:
                    if changes['date_iso'] is None: data.update(date_iso=None,date_mmdd=None)
                    else:
                        d=date.fromisoformat(changes['date_iso']); data['date_mmdd']=d.strftime('%m.%d')
                        if changes.get('date_mmdd') not in (None,data['date_mmdd']): raise Rejected('date_mismatch')
                elif 'date_mmdd' in changes and data.get('date_iso'):
                    if date.fromisoformat(data['date_iso']).strftime('%m.%d') != changes['date_mmdd']: raise Rejected('full_date_required')
                if data.get('finance_cost') and ('amount_rub' in changes or 'amount_expression' in changes) and not m.get('_finance_cost'):
                    if changes.get('amount_rub') != data['finance_cost']['work_amount']:
                        raise Rejected('use_set_project_cost_to_preserve_tax_breakdown')
                if m.get('_finance_cost'):data['finance_cost']=m['_finance_cost']
                if 'amount_rub' in changes:
                    data.pop('amount_breakdown',None);data['amount_rub']=_money(changes['amount_rub']) if changes['amount_rub'] is not None else None
                if 'amount_expression' in changes:
                    expression=_text(changes['amount_expression'],500,False)
                    parsed=amount_breakdown('Project - '+expression)
                    if not parsed['complete']: raise Rejected('ambiguous_amount_expression')
                    data['amount_breakdown']=parsed;data['amount_rub']=parsed['total']
                if creating:
                    # Preserve the existing full-import and duplicate safeguards.
                    imported=tx.one("SELECT 1 AS ok FROM public.tm_project_import_state WHERE group_id=%s AND instance_id=%s AND list_id=%s AND status='complete' AND last_snapshot_at>now()-interval '5 minutes'",(g['id'],self.instance,g.get('reminder_list_id')))
                    if not imported and not m.get('_server_managed'): raise Rejected('fresh_reminder_import_required')
                    peers=tx.all("SELECT id,title,project_data FROM public.tasks WHERE context_group_id=%s AND record_kind='project' AND project_archived_at IS NULL",(g['id'],))
                    if possible_duplicates(data,peers): raise Rejected('possible_duplicate_project')
                    data.update(work_status='planned',display_protocol='v18_payment_window')
                data['label']=_text(data.get('label'),350,False)
                row['project_data']=data
                try: row['title']=render_title(data)
                except RuntimeError: raise Rejected('invalid_project_title') from None
            elif op=='set_project_status':
                strict_fields(changes,('status',),('status',))
                if changes['status'] not in ('planned','in_progress','delivered'): raise Rejected('invalid_project_status')
                if data.get('request_state') in ('pending','rejected'): raise Rejected('confirm_project_request_first')
                data['work_status']=changes['status'];row['project_data']=data
                row['status']='completed' if changes['status']=='delivered' else 'open'
            else:
                strict_fields(changes,('payment_status',),('payment_status',))
                state=changes['payment_status']
                if state not in ('unknown','awaiting','partial','paid'): raise Rejected('invalid_payment_status')
                if row['payment_status']=='paid' and state!='paid': raise Rejected('paid_reversal_requires_reconciliation')
                if state=='paid' and not any(e['kind']=='user' for e in m['evidence']): raise Rejected('payment_requires_direct_user_evidence')
                row['payment_status']=state
                if state=='paid':
                    row['payment_evidence']={'user_confirmed':True,'source':'v24_explicit_user','evidence':m['evidence'],'removal_authorized':False}
                    # Time is assigned by the transaction, not guessed from conversation.
                    row['payment_confirmed_at']=row.get('payment_confirmed_at') or 'assigned_at_commit'
            row['title']=tx.one('SELECT public.tm_payment_title_v18(%s,%s,%s::jsonb,(now() AT TIME ZONE \'Asia/Yekaterinburg\')::date) AS title',(row['title'],row['payment_status'],encoded(row['project_data'])))['title']
            if data.get('request_state')=='pending' and not row['title'].startswith('➕'): row['title']='➕ '+row['title']
            if data.get('request_state')=='rejected' and not row['title'].startswith('Отклонено:'): row['title']='Отклонено: '+row['title']
        else: raise Rejected('unsupported_business_operation')
        return {'entity':'project' if row['record_kind']=='project' else 'task','operation':op,
                'target_id':row['id'],'before':before,'after':task_state(row),'group_id':g['id']}

    def execute(self, tx, m, plan):
        if m['operation']=='update_payment': return self.payment_execute(tx,m,plan)
        r=plan['after'];ident=plan['target_id'];creating=ident is None
        if m['operation']=='archive_project':
            # Archive is intentionally isolated from the generic project update
            # path. It changes only canonical archival metadata; managed
            # Reminder projection is handled afterwards by ManagedBusiness.
            result=tx.one(
                'UPDATE public.tasks SET project_data=%s::jsonb,project_archived_at=clock_timestamp(),updated_at=clock_timestamp() '
                'WHERE id=%s AND project_archived_at IS NULL RETURNING id,updated_at,project_archived_at',
                (encoded(r['project_data']),ident))
            if not result:
                raise Rejected('project_already_archived')
            after=copy.deepcopy(r);after['project_archived_at']=result['project_archived_at']
            tx.execute(
                'INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence) '
                'VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)',
                (ident,'archive_project','v24:user_confirmed',encoded(plan['before']),encoded(after),encoded(m['evidence'])))
            return {'entity':'project','id':ident,'revision':result['updated_at'],
                    'native_sync':'pending','payment_removal_authorized':False,'archived':True}
        json_fields=('project_data','payment_evidence','reminder_due_spec')
        columns=(PROJECT_COLUMNS+(('tags',) if plan.get('workspace_id') else ())) if plan['entity']=='project' else TASK_COLUMNS
        if creating:
            columns=('title','description','status','due_at','reminder_due_spec','tags','record_kind','target_app','context_group_id','project_data','payment_status')
        values=[]; slots=[]
        for k in columns:
            if k in ('payment_confirmed_at','project_archived_at') and r[k]=='assigned_at_commit':
                slots.append('clock_timestamp()');continue
            slots.append('%s::jsonb' if k in json_fields else '%s');values.append(encoded(r[k]) if k in json_fields else r[k])
        # Identifiers come only from module constants, never client input.
        if creating:
            source=next((e for e in m['evidence'] if e['kind']=='telegram'),{})
            result=tx.one('INSERT INTO public.tasks('+','.join(columns)+',source_chat_id,source_message_id,completed_at,cancelled_at) VALUES('+','.join(slots)+",%s,%s,CASE WHEN %s='completed' THEN clock_timestamp() END,CASE WHEN %s='cancelled' THEN clock_timestamp() END) RETURNING id,updated_at",values+[source.get('chat_id'),source.get('message_id'),r['status'],r['status']])
        else:
            assignments=','.join(k+'='+slot for k,slot in zip(columns,slots))
            result=tx.one('UPDATE public.tasks SET '+assignments+",completed_at=CASE WHEN %s='completed' THEN coalesce(completed_at,clock_timestamp()) ELSE NULL END,cancelled_at=CASE WHEN %s='cancelled' THEN coalesce(cancelled_at,clock_timestamp()) ELSE NULL END,updated_at=clock_timestamp() WHERE id=%s RETURNING id,updated_at",values+[r['status'],r['status'],ident])
        ident=result['id']
        if plan['entity']=='project':
            for ev in m['evidence']:
                if ev['kind']=='telegram': tx.execute('INSERT INTO public.tm_project_links(task_id,chat_id,message_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(ident,ev['chat_id'],ev['message_id']))
            tx.execute('INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)',(ident,m['operation'],'v24:user_confirmed',encoded(plan['before']),encoded(plan['after']),encoded(m['evidence'])))
        return {'entity':plan['entity'],'id':ident,'revision':result['updated_at'],'native_sync':'pending','payment_removal_authorized':False}

    def payment_plan(self, tx, m, lock):
        strict_fields(m['changes'],('amount','allocations','reason'),('amount','allocations','reason'))
        original=tx.one('SELECT p.* FROM public.tm_payment_events p JOIN public.telegram_chat_groups g ON g.id=p.group_id WHERE p.id=%s::uuid AND '+scope.SCOPE+(' FOR SHARE OF p' if lock else ''),(m['target_id'],self.instance))
        if not original: raise Rejected('payment_not_found')
        check_revision(original['created_at'],m['expected_revision'])
        if original['kind'] not in ('received','refund') or original['state']!='confirmed': raise Rejected('only_confirmed_receipts_can_be_corrected')
        if tx.one('SELECT original_id FROM tm_v24.payment_corrections WHERE original_id=%s::uuid',(original['id'],)): raise Rejected('payment_already_corrected')
        scope.evidence(tx,self.instance,m['evidence'],original['group_id'],lock)
        if not any(e['kind']=='user' for e in m['evidence']): raise Rejected('correction_requires_direct_user_evidence')
        value=_money(m['changes']['amount'])
        if Decimal(value)<=0: raise Rejected('invalid_amount')
        allocations=m['changes']['allocations'];_text(m['changes']['reason'],2000,False)
        if not isinstance(allocations,list) or not 1<=len(allocations)<=100: raise Rejected('invalid_allocations')
        old=tx.all('SELECT task_id,work_key,amount FROM public.tm_payment_allocations WHERE payment_id=%s::uuid ORDER BY task_id,work_key',(original['id'],))
        revisions={};total=Decimal(0);seen=set()
        for allocation in old+allocations:
            strict_fields(allocation,('task_id','work_key','amount'),('task_id','amount'))
            t=scope.task(tx,self.instance,allocation['task_id'],lock);scope.no_salary(tx,t['id'])
            if t['record_kind']!='project' or t['context_group_id']!=original['group_id'] or t.get('project_archived_at'): raise Rejected('allocation_project_not_current')
            revisions[str(t['id'])]=t['updated_at']
        for allocation in allocations:
            key=(allocation['task_id'],allocation.get('work_key','main'))
            if key in seen: raise Rejected('duplicate_allocation')
            seen.add(key);n=Decimal(_money(allocation['amount']))
            if n<=0: raise Rejected('invalid_amount')
            total+=n
        if total>Decimal(value): raise Rejected('allocations_exceed_payment')
        return {'entity':'payment','operation':m['operation'],'target_id':original['id'],'group_id':original['group_id'],
                'before':{'event':original,'allocations':old,'project_revisions':revisions},
                'after':{'correction':'append_offset_and_replacement','amount':value,'allocations':allocations,'reason':m['changes']['reason']}}

    def payment_execute(self, tx, m, plan):
        original=plan['before']['event'];g=tx.one('SELECT group_key FROM public.telegram_chat_groups WHERE id=%s',(original['group_id'],))
        now=tx.one('SELECT now() AS value')['value']; results=[]
        for index,(kind,value,allocations) in enumerate([
            ('refund' if original['kind']=='received' else 'received',original['amount'],plan['before']['allocations']),
            (original['kind'],plan['after']['amount'],plan['after']['allocations'])]):
            ev={'kind':kind,'amount':value,'currency':original['currency'],'method':original.get('method') or 'unknown','counterparty':original.get('counterparty'),'occurred_at':now,'state':'confirmed'}
            res=tx.one('SELECT public.tm_record_payment_v14(%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,true) AS result',('v24-correction:'+original['id']+':'+str(index),g['group_key'],encoded(ev),encoded(allocations),encoded(m['evidence'])))['result'];results.append(res)
        tx.execute('INSERT INTO tm_v24.payment_corrections(original_id,offset_id,replacement_id,reason) VALUES(%s::uuid,%s::uuid,%s::uuid,%s)',(original['id'],results[0]['id'],results[1]['id'],plan['after']['reason']))
        return {'entity':'payment','original_id':original['id'],'offset_id':results[0]['id'],'replacement_id':results[1]['id'],'history_preserved':True,'project_payment_status_changed':False}
