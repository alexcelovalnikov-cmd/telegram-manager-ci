"""Apply server rules/templates/destinations around the preserved business model."""
from copy import deepcopy
from datetime import date
from uuid import uuid5,NAMESPACE_URL
from tm_api.v24.business import Business
from tm_api.v24.common import Rejected,encoded
from tm_calendar import repository as calendars
from . import repository as repo,finance

class ManagedBusiness:
    def __init__(self,instance):self.instance=instance;self.legacy=Business(instance)
    def locate(self,tx,m):
        if m.get('workspace_id'):return repo.workspace(tx,self.instance,m['workspace_id'],active=True)
        if m.get('group_key'):
            return tx.one('SELECT w.* FROM tm_config.workspaces w JOIN public.telegram_chat_groups g ON g.id=w.group_id WHERE w.instance_id=%s AND g.group_key=%s',(self.instance,m['group_key']))
        if m.get('target_id') and str(m['target_id']).isdigit():
            return tx.one('SELECT w.* FROM tm_config.workspaces w JOIN public.tasks t ON t.context_group_id=w.group_id WHERE w.instance_id=%s AND t.id=%s',(self.instance,int(m['target_id'])))
        return None
    def scoped(self,tx,m):
        w=self.locate(tx,m);copy=deepcopy(m)
        if not w:return copy,None
        ctx=repo.context(tx,self.instance,w['id']);repo.assert_context(ctx,m)
        g=tx.one('SELECT group_key FROM public.telegram_chat_groups WHERE id=%s',(w['group_id'],))
        if m.get('group_key') not in (None,g['group_key']):raise Rejected('workspace_group_mismatch')
        copy['group_key']=g['group_key'];copy['_server_managed']=w['settings']['reminder_list']['mode'] in ('server_new','server_existing')
        return copy,ctx
    def legacy_task(self,tx,ident):
        from tm_api.v24.scope import task
        return task(tx,self.instance,ident)
    def receipts(self,tx,task_id):
        if task_id is None:return []
        return tx.all('SELECT p.id,p.kind,p.state,a.amount,public.tm_evidence_valid_v14(p.group_id,p.evidence) AS evidence_valid FROM public.tm_payment_events p JOIN public.tm_payment_allocations a ON a.payment_id=p.id WHERE a.task_id=%s ORDER BY p.id,a.work_key',(task_id,))
    def plan(self,tx,m,lock=False):
        working,ctx=self.scoped(tx,m)
        if ctx and m['operation']=='set_payment_state' and m['changes'].get('payment_status') in ('paid','partial'):
            row=self.legacy_task(tx,m['target_id'])
            ledger=self.receipts(tx,row['id']);value=(row['project_data'].get('finance_cost') or {}).get('work_amount',row['project_data'].get('amount_rub'))
            if finance.balance(value,ledger)['payment_status']!=m['changes']['payment_status']:
                raise Rejected('record_actual_net_receipt_before_payment_state')
        p=self.legacy.plan(tx,working,lock)
        return self.decorate(tx,m,p,ctx)
    def decorate(self,tx,m,p,ctx):
        if not ctx:return p
        a=p['after'];settings=ctx['workspace']['settings'];dest=settings['reminder_list']
        if dest['mode']=='none':raise Rejected('assign_workspace_destination_first')
        p=deepcopy(p);a=p['after'];fmt=repo.formatting(ctx)
        old=tx.one('SELECT * FROM tm_config.reminder_bindings WHERE task_id=%s',(a['id'],)) if a.get('id') else None
        base_title=m['changes'].get('title',old['base_title'] if old else a['title'])
        base_description=m['changes'].get('description',old['base_description'] if old else a.get('description') or '')
        kind='project' if p['entity']=='project' else (m.get('analysis') or {}).get('entity_kind','task')
        a['tags']=list(dict.fromkeys(t for t in (a.get('tags',[])+settings.get('default_tags',[])) if t))
        values={'title':base_title,'description':base_description,'label':base_title,
          'tags':fmt['tag_separator'].join(t if t.startswith('#') else '#'+t for t in a.get('tags',[])),
          'workspace':{'name':ctx['workspace']['name'],'key':ctx['workspace']['workspace_key']},
          'status':a['status'],'date':'','date_prefix':'','amount_separator':'','payment_display':'','payment_marker':'',
          'received':'','remaining':'','tax':'','document_total':'','amount':''}
        if p['entity']=='project':
            data=a['project_data'];stored=data.get('finance_cost') or {};work=stored.get('work_amount',data.get('amount_rub'))
            receipts=self.receipts(tx,a.get('id'));fin=finance.balance(work,receipts)
            p['ledger_basis']=receipts;values.update(label=data['label'],title=data['label'],description=data.get('server_description',''),
               amount=finance.compact(fin['work_amount'],fmt),received=finance.compact(fin['received_net'],fmt),
               remaining=finance.compact(fin['remaining'],fmt),payment_display=finance.display(fin,fmt),
               tax=finance.compact(stored.get('tax_amount'),fmt),document_total=finance.compact(stored.get('document_total'),fmt))
            if values['payment_display']:values['amount_separator']=' - '
            if a['title'].startswith('⚡') and fmt.get('awaiting_marker'):values['payment_marker']=fmt['awaiting_marker']+' '
            when=data.get('date_iso')
            if when:
                d=date.fromisoformat(when);values['date']=d.strftime({'MM.DD':'%m.%d','DD.MM':'%d.%m','YYYY-MM-DD':'%Y-%m-%d'}[fmt['date_format']])
            else:values['date']=data.get('date_mmdd') or ''
            if values['date']:values['date_prefix']=values['date']+' '
        presentation=repo.present(ctx,kind,values)
        if old and old.get('manual_title') and 'title' not in m['changes']:presentation['title']=old['manual_title']
        if old and old.get('manual_description') is not None and 'description' not in m['changes']:presentation['description']=old['manual_description']
        if not p['target_id'] and p['entity']=='task':
            duplicate=tx.one("SELECT id FROM public.tasks WHERE context_group_id=%s AND record_kind='task' AND status IN ('open','waiting') AND lower(btrim(title))=lower(btrim(%s)) LIMIT 1",(a['context_group_id'],presentation['title']))
            if duplicate:raise Rejected('possible_duplicate_formatted_task')
        a['title']=presentation['title']
        if p['entity']=='task':a['description']=presentation['description']
        if dest['mode'] in ('server_new','server_existing'):
            c=calendars.access(tx,calendars.api_user(tx,self.instance),dest['id'],write=True)
            if c.get('component_type')!='VTODO':raise Rejected('workspace_destination_not_todo')
            a['target_app']=None
            p['destination']={'mode':'server','list_id':c['id'],'component_type':'VTODO'}
        else:p['destination']={'mode':'legacy','list_id':dest['id'],'mac_required':True}
        p['configuration_token']=ctx['configuration_token'];p['workspace_id']=ctx['workspace']['id']
        p['binding_state']=old
        if old:
            p['reminder_state']=tx.one('SELECT id,calendar_id,revision,etag,deleted_at,fields FROM tm_calendar.events WHERE id=%s::uuid',(old['resource_id'],))
        p['_base_title']=base_title;p['_base_description']=base_description;p['_presentation_description']=presentation['description']
        return p
    def execute(self,tx,m,p):
        repo.actor(tx,self.instance)
        r=self.legacy.execute(tx,m,p)
        if p.get('workspace_id'):
            self.provenance(tx,m,p,r['id'])
            if p['destination']['mode']=='server':
                self.project(tx,m,p,r['id']);r['native_sync']='not_used';r['server_sync']='consistent'
                row=tx.one('SELECT updated_at FROM public.tasks WHERE id=%s',(r['id'],));r['revision']=row['updated_at']
        return r
    def provenance(self,tx,m,p,ident):
        analysis=m.get('analysis') or {}
        tx.execute('INSERT INTO tm_config.analysis_receipts(instance_id,workspace_id,configuration_token,operation,target_id,rule_refs,reason,evidence) VALUES(%s,%s::uuid,%s,%s,%s,%s::jsonb,%s,%s::jsonb)',
            (self.instance,p['workspace_id'],p['configuration_token'],m['operation'],str(ident),encoded(analysis.get('rule_refs',[])),str(analysis.get('reason','explicit_user_mutation')),encoded(m['evidence'])))
    def project(self,tx,m,p,ident):
        from tm_reminders import codec
        from tm_reminders.models import Reminder
        user=calendars.api_user(tx,self.instance);a=p['after']
        tx.execute("UPDATE public.tasks SET target_app=NULL,reminder_active=false,external_system='tm_server_reminders',updated_at=clock_timestamp() WHERE id=%s",(ident,))
        old=tx.one('SELECT * FROM tm_config.reminder_bindings WHERE task_id=%s',(ident,))
        rid=old['resource_id'] if old else str(uuid5(NAMESPACE_URL,'tm-task:'+self.instance+':'+str(ident)))
        previous=tx.one('SELECT * FROM tm_calendar.events WHERE id=%s::uuid',(rid,)) if old else None

        # Archived projects remain canonical server history but disappear from
        # the user Reminder projection.
        if p['entity']=='project' and a.get('project_archived_at'):
            if previous and previous.get('deleted_at') is None:
                # Internal projection cleanup must not re-enter the inbound
                # CalDAV bridge and mutate the same project a second time.
                calendars.delete(tx,user,rid,previous['revision'],bridge=False)
                tx.execute('UPDATE tm_config.reminder_bindings SET hidden=true WHERE task_id=%s',(ident,))
            elif old:
                tx.execute('UPDATE tm_config.reminder_bindings SET hidden=true WHERE task_id=%s',(ident,))
            return

        # Fully paid projects are server history, not completed Reminder rows.
        # Delete only the projection; canonical project/ledger/history remain.
        if p['entity']=='project' and a.get('payment_status')=='paid':
            if previous and previous.get('deleted_at') is None:
                calendars.delete(tx,user,rid,previous['revision'])
            elif old:
                tx.execute('UPDATE tm_config.reminder_bindings SET hidden=true WHERE task_id=%s',(ident,))
            tx.execute(
                "UPDATE public.tasks SET payment_evidence=coalesce(payment_evidence,'{}'::jsonb) || %s::jsonb WHERE id=%s",
                (encoded({'reminder_projection':'hidden_paid'}),ident))
            return

        if old and old['hidden']:
            before_payment=(p.get('before') or {}).get('payment_status')
            before_evidence=(p.get('before') or {}).get('payment_evidence') or {}
            payment_owned_hidden=(
                p['entity']=='project'
                and before_payment=='paid'
                and before_evidence.get('reminder_projection')=='hidden_paid'
                and a.get('payment_status')!='paid'
            )
            if not payment_owned_hidden:
                return
            tx.execute('UPDATE tm_config.reminder_bindings SET hidden=false WHERE task_id=%s',(ident,))
            old=dict(old);old['hidden']=False
        # A ledger correction may move a previously paid project back to
        # partial. refresh_project clears the payment-owned hidden flag; restore
        # the deterministic VTODO before projecting the partial state.
        if previous and previous.get('deleted_at') is not None:
            before=calendars.event_public(previous)
            previous=tx.one(
                "UPDATE tm_calendar.events SET deleted_at=NULL,revision=revision+1,updated_by=%s,"
                "updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING *",
                (user,rid))
            calendars.changed(tx,previous['calendar_id'],previous['href'],False)
            calendars.calendar_audit(tx,user,'restore_paid_project_reminder',rid,before,calendars.event_public(previous))
        uid=previous['caldav_uid'] if previous else rid+'@telegram-manager'
        due=a.get('due_at') if p['entity']=='task' else None
        preserved=previous['fields'] if previous else {}
        if p['entity']=='task' and 'due_at' not in m['changes'] and 'reminder_due_spec' not in m['changes']:
            due=preserved.get('due_at',due)
        fields=Reminder(title=a['title'],description=p.get('_presentation_description',a.get('description') or ''),due_at=due,all_day=preserved.get('all_day',False) if 'due_at' not in m['changes'] else False,priority=preserved.get('priority',0),recurrence=preserved.get('recurrence'),
            timezone=repo.workspace(tx,self.instance,p['workspace_id'])['settings']['timezone'],
            status={'completed':'completed','cancelled':'cancelled','waiting':'in_progress'}.get(a['status'],'open'),categories=a.get('tags',[])).model_dump()
        changed={k for k,v in fields.items() if not previous or previous['fields'].get(k)!=v}
        text=codec.encode(fields,uid,previous['icalendar'] if previous else None,changed=changed)
        calendars.put(tx,user,p['destination']['list_id'],previous['href'] if previous else rid+'.ics',text,
            expected_revision=previous['revision'] if previous else None,ident=rid,source='telegram_manager')
        if not old:
            tx.execute('INSERT INTO tm_config.reminder_bindings(task_id,resource_id,workspace_id,base_title,base_description) VALUES(%s,%s::uuid,%s::uuid,%s,%s)',
                (ident,rid,p['workspace_id'],p['_base_title'],p['_base_description']))
        else:
            tx.execute('UPDATE tm_config.reminder_bindings SET base_title=%s,base_description=%s,manual_title=CASE WHEN %s THEN NULL ELSE manual_title END,manual_description=CASE WHEN %s THEN NULL ELSE manual_description END WHERE task_id=%s',
                (p['_base_title'],p['_base_description'],'title' in m['changes'],'description' in m['changes'],ident))
