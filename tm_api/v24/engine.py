"""Immutable previews, exact confirmation, CAS, evidence and atomic receipts."""
import hashlib
import uuid
from datetime import datetime,timezone
from .common import Rejected, digest, diff, encoded
from .models import Mutation
from .business import Business
from . import scope

# Compatibility marker retained for V69/V73 contract tests and old clients:
# managed_reconciliation_postproduction_only now maps to the broader profile guard below.

RECONCILIATION_AUTO_OPS = {
    'create_task', 'update_task', 'complete_task',
    'create_project', 'update_project', 'set_project_status',
    'create_calendar_event', 'update_calendar_event', 'move_calendar_event',
    'create_reminder', 'update_reminder', 'complete_reminder', 'move_reminder',
    'set_project_cost', 'create_payment_expectation', 'update_payment_expectation',
    'create_managed_entity', 'update_managed_entity',
}

class Engine:
    def __init__(self,database,instance,configurable=False):
        self.database,self.instance=database,instance
        self.business=Business(instance)
        from tm_calendar.business import CalendarBusiness
        self.calendar=CalendarBusiness(instance)
        self.whatsapp_backfill=None
        self.postproduction_sheet=None
        self.postproduction_reminders=None
        self.pov_accounting=None
        self.configurable=configurable
        if configurable:
            from ..v25.business import ConfigBusiness
            from ..v25.managed import ManagedBusiness
            from ..v25.payments import FinanceBusiness
            from tm_reminders.business import RemindersBusiness
            self.configuration=ConfigBusiness(instance)
            self.managed=ManagedBusiness(instance)
            self.finance=FinanceBusiness(instance,self.managed)
            self.reminders=RemindersBusiness(instance)
            from ..v25.media import MediaBusiness
            self.media=MediaBusiness(instance)
    def _key_lock(self,tx,key):
        tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('tm-v24:'+self.instance+':'+key,))
    def receipt(self,tx,operation,key,arguments):
        self._key_lock(tx,key)
        row=tx.one('SELECT input_digest,result FROM tm_v24.operations WHERE instance_id=%s AND operation=%s AND request_key=%s',(self.instance,operation,key))
        if row:
            if row['input_digest']!=digest(arguments): raise Rejected('request_key_collision')
            return row['result']
        return None
    def save_receipt(self,tx,operation,key,arguments,result):
        tx.execute('INSERT INTO tm_v24.operations(instance_id,operation,request_key,input_digest,result) VALUES(%s,%s,%s,%s,%s::jsonb)',(self.instance,operation,key,digest(arguments),encoded(result)))
    def planner(self,m):
        from ..v25.business import CONFIG_OPS,ENTITY_OPS
        from ..v25.payments import OPS as FINANCE_OPS
        from tm_reminders.business import OPS as REMINDER_OPS
        from ..v25.media import OPS as MEDIA_OPS
        from ..whatsapp_backfill import OPS as WHATSAPP_BACKFILL_OPS
        from ..postproduction_sheet_write import OPS as POSTPRODUCTION_SHEET_OPS
        from ..postproduction_reminder_projection import OPS as POSTPRODUCTION_REMINDER_OPS
        from ..postproduction_pov_accounting import OPS as POV_ACCOUNTING_OPS
        operation=m['operation']
        if operation in POV_ACCOUNTING_OPS:
            if self.pov_accounting is None:
                raise Rejected('pov_accounting_projection_disabled')
            return self.pov_accounting
        if operation in POSTPRODUCTION_REMINDER_OPS:
            if self.postproduction_reminders is None:
                raise Rejected('postproduction_reminder_projection_disabled')
            return self.postproduction_reminders
        if operation in POSTPRODUCTION_SHEET_OPS:
            if self.postproduction_sheet is None:
                raise Rejected('postproduction_sheet_write_extension_disabled')
            return self.postproduction_sheet
        if operation in WHATSAPP_BACKFILL_OPS:
            if self.whatsapp_backfill is None:
                raise Rejected('whatsapp_backfill_extension_disabled')
            return self.whatsapp_backfill
        if operation=='update_payment' and not self.configurable:return self.business
        if operation in CONFIG_OPS|ENTITY_OPS|FINANCE_OPS|REMINDER_OPS|MEDIA_OPS:
            if not self.configurable:raise Rejected('v25_configuration_extension_disabled')
            if operation in CONFIG_OPS|ENTITY_OPS:return self.configuration
            if operation in FINANCE_OPS:return self.finance
            if operation in MEDIA_OPS:return self.media
            return self.reminders
        if self.configurable and not operation.startswith(('create_calendar','update_calendar','delete_calendar','set_calendar')) and operation!='update_payment':
            return self.managed
        return self.calendar if m['operation'].startswith(('create_calendar','update_calendar','delete_calendar','set_calendar')) else self.business
    def transaction_lock(self,tx,write):
        if self.configurable:
            from tm_calendar.repository import lock
            lock(tx,write=write)

    def record_payment_compat(self,request_key,payload):
        """Retry-safe bridge from legacy record_payment to the V25 ledger."""
        if not self.configurable or payload.get('confirmed') is not True:
            raise Rejected('explicit_confirmation_required')
        event=payload.get('event') or {}
        if event.get('kind') not in ('received','refund'):
            raise Rejected('receipt_kind_must_be_received_or_refund')
        arguments={'request_key':request_key,'payload':payload}
        legacy_key='v19:'+hashlib.sha256(
            (self.instance+':'+request_key).encode()).hexdigest()
        with self.database.transaction() as tx:
            self.transaction_lock(tx,True)
            saved=self.receipt(tx,'record_payment_compat',request_key,arguments)
            if saved is not None:
                return saved
            group=scope.group(tx,self.instance,payload.get('group_key'),lock=True)
            legacy=tx.one(
                'SELECT id,group_id,kind,amount,currency,method,counterparty,external_ref,'
                'occurred_at,state,user_confirmed,evidence FROM public.tm_payment_events '
                'WHERE request_key=%s',(legacy_key,))
            if legacy:
                self._validate_legacy_payment_retry(tx,legacy,group,payload)
                result={'applied':True,'entity':'payment','id':legacy['id'],
                        'recovered_existing':True,'duplicate_prevented':True}
                self.save_receipt(
                    tx,'record_payment_compat',request_key,arguments,result)
                return result
            dedupe='record-payment-compat.'+hashlib.sha256(
                (self.instance+':'+request_key).encode()).hexdigest()
            mutation={
                'operation':'create_payment','dedupe_key':dedupe,
                'group_key':payload.get('group_key'),
                'changes':{
                    'kind':event['kind'],'amounts':{'work_amount':event['amount']},
                    'allocations':[{
                        'task_id':a['task_id'],'work_key':a.get('work_key','main'),
                        'amount':a['amount'],'expected_revision':a['expected_updated_at']}
                        for a in payload.get('allocations') or []],
                    'occurred_at':event['occurred_at'],'external_ref':event.get('external_ref'),
                    'method':event.get('method','unknown'),'counterparty':event.get('counterparty')},
                'evidence':payload.get('evidence') or [],
                'analysis':{'reason':'legacy record_payment compatibility'},
                '_record_payment_confirmed':True,
            }
            workspace=self.managed.locate(tx,{'group_key':payload.get('group_key')})
            if workspace:
                from ..v25 import repository as config
                ctx=config.context(tx,self.instance,workspace['id'])
                mutation['workspace_id']=workspace['id']
                mutation['configuration_token']=ctx['configuration_token']
            plan=self.finance.plan(tx,mutation,lock=True)
            if plan['after']['group_id']!=group['id']:
                raise Rejected('project_changed_or_out_of_scope')
            result=self.finance.execute(tx,mutation,plan)
            scope.audit(tx,self.instance,'record_payment',result.get('id'),
                        None,plan['after'],mutation['evidence'])
            public={'applied':True,**result,'compatibility':'v25_direct_ledger'}
            self.save_receipt(
                tx,'record_payment_compat',request_key,arguments,public)
            return public

    @staticmethod
    def _validate_legacy_payment_retry(tx,legacy,group,payload):
        from ..v25 import finance
        event=payload['event']
        expected=sorted(
            (int(a['task_id']),a.get('work_key','main'),
             str(finance.money(a['amount']))) for a in payload['allocations'])
        actual=sorted(
            (int(a['task_id']),a.get('work_key') or 'main',
             str(finance.money(str(a['amount']))))
            for a in tx.all(
                'SELECT task_id,work_key,amount FROM public.tm_payment_allocations '
                'WHERE payment_id=%s::uuid',(legacy['id'],)))
        same=(legacy['group_id']==group['id']
              and legacy['kind']==event['kind']
              and str(finance.money(str(legacy['amount'])))==str(finance.money(event['amount']))
              and legacy['currency']==event.get('currency','RUB')
              and legacy.get('method')==event.get('method','unknown')
              and legacy.get('counterparty')==event.get('counterparty')
              and legacy.get('external_ref')==event.get('external_ref')
              and legacy.get('state')=='confirmed'
              and legacy.get('user_confirmed') is True
              and legacy.get('evidence')==payload.get('evidence')
              and actual==expected)
        if not same:
            raise Rejected('record_payment_retry_collision')

    def plan(self,tx,m,lock=False):
        ctx=None
        if self.configurable and m.get('workspace_id'):
            from ..v25.business import CONFIG_OPS
            if m['operation'] not in CONFIG_OPS:
                from ..v25.repository import guard_token
                ctx=guard_token(tx,self.instance,m)
        plan=self.planner(m).plan(tx,m,lock=lock)
        if ctx:
            plan['workspace_id']=ctx['workspace']['id']
            plan['configuration_token']=ctx['configuration_token']
        return plan
    @staticmethod
    def public_plan(plan):
        return {k:v for k,v in plan.items() if not k.startswith('_')}
    def preview(self,request_key,mutations):
        if not 1<=len(mutations)<=20: raise Rejected('batch_size_limit')
        payload=[Mutation.model_validate(m).model_dump(mode='json') if isinstance(m,dict) else m.model_dump(mode='json') for m in mutations]
        # One target can only be changed once in a confirmation batch.
        ids=[(m.get('calendar_id'),('task' if m['operation'] in ('update_task','complete_task','update_project','set_project_status','set_payment_state','set_project_cost') else 'configuration' if m['operation'].endswith(('rule','template','entity_type')) else 'resource'),m['target_id']) for m in payload if m['target_id'] is not None]
        if len(set(ids))!=len(ids): raise Rejected('combine_changes_for_same_target')
        creating=[m['dedupe_key'] for m in payload if m['operation'].startswith('create_')]
        if len(set(creating))!=len(creating): raise Rejected('duplicate_candidate_in_batch')
        with self.database.transaction() as tx:
            self.transaction_lock(tx,False)
            self._key_lock(tx,request_key)
            existing=tx.one('SELECT * FROM tm_v24.previews WHERE instance_id=%s AND request_key=%s',(self.instance,request_key))
            if existing:
                if existing['payload_digest']!=digest(payload): raise Rejected('request_key_collision')
                return self._preview_public(existing)
            plans=[]
            for m in payload:
                scope.evidence(tx,self.instance,m['evidence'])
                if m['operation'].startswith('create_') and tx.one('SELECT target_id FROM tm_v24.identities WHERE instance_id=%s AND dedupe_key=%s',(self.instance,m['dedupe_key'])):
                    raise Rejected('candidate_already_created')
                plans.append(self.plan(tx,m))
            public=[self.public_plan(p) for p in plans]
            changes=[{'operation':p['operation'],'entity':p['entity'],'target_id':p['target_id'],
                'before':p['before'],'after':p['after'],'diff':diff(p['before'],p['after'])} for p in public]
            preview_id=str(uuid.uuid4());confirmation_digest=digest({'preview_id':preview_id,'mutations':payload,'plans':plans})
            row=tx.one("INSERT INTO tm_v24.previews(id,instance_id,request_key,payload_digest,payload,plans,changes,preview_digest,expires_at) VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,now()+interval '30 minutes') RETURNING *",(preview_id,self.instance,request_key,digest(payload),encoded(payload),encoded(plans),encoded(changes),confirmation_digest))
            return self._preview_public(row)
    @staticmethod
    def _preview_public(row):
        return {'preview_id':row['id'],'request_key':row['request_key'],'preview_digest':row['preview_digest'],
            'changes':row['changes'],'evidence':[m['evidence'] for m in row['payload']],
            'expires_at':row['expires_at'],'status':row['status'],'requires_confirmation':row['status']=='preview',
            'applied':row['status']=='applied','atomic':True}
    def get_preview(self,tx,preview_id,lock=True):
        suffix=' FOR UPDATE' if lock else ''
        row=tx.one('SELECT * FROM tm_v24.previews WHERE id=%s::uuid AND instance_id=%s'+suffix,(preview_id,self.instance))
        if not row: raise Rejected('preview_not_found')
        return row
    def apply_in_transaction(self,tx,preview_id,preview_digest,confirmation_ref):
        self.transaction_lock(tx,True)
        row=self.get_preview(tx,preview_id)
        if row['preview_digest']!=preview_digest: raise Rejected('preview_digest_mismatch')
        if row['status']=='applied': return row['result']
        if row['status']!='preview' or datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc):
            raise Rejected('preview_expired')
        if not isinstance(confirmation_ref,str) or len(confirmation_ref)<8: raise Rejected('confirmation_required')
        self._key_lock(tx,'business')
        checked=[]
        for m,p in zip(row['payload'],row['plans']):
            scope.evidence(tx,self.instance,m['evidence'],lock=True)
            if m['operation'].startswith('create_'):
                if tx.one('SELECT target_id FROM tm_v24.identities WHERE instance_id=%s AND dedupe_key=%s',(self.instance,m['dedupe_key'])):
                    raise Rejected('candidate_already_created')
            current=self.plan(tx,m,lock=True)
            if digest(self.public_plan(current))!=digest(self.public_plan(p)):
                raise Rejected('preview_state_changed')
            checked.append((m,p))
        results=[]
        for m,p in checked:
            # Previous effects in this batch can expose duplicate creation or alter
            # allocation revisions. Revalidate inside the same rollback boundary.
            current=self.plan(tx,m,lock=True)
            if digest(self.public_plan(current))!=digest(self.public_plan(p)):
                raise Rejected('batch_dependency_requires_new_preview')
            result=self.planner(m).execute(tx,m,p);results.append(result)
            if self.configurable and p.get('workspace_id') and not p.get('destination'):
                self.managed.provenance(tx,m,p,result.get('id') or p['target_id'])
            if m['operation'].startswith('create_'):
                ident=result.get('id') or result.get('record',{}).get('id')
                tx.execute('INSERT INTO tm_v24.identities(instance_id,dedupe_key,entity,target_id) VALUES(%s,%s,%s,%s)',(self.instance,m['dedupe_key'],p['entity'],str(ident)))
            scope.audit(tx,self.instance,m['operation'],result.get('id') or p['target_id'],p['before'],p['after'],m['evidence'])
        result={'applied':True,'preview_id':preview_id,'results':results,'atomic':True}
        tx.execute("UPDATE tm_v24.previews SET status='applied',confirmation_ref=%s,result=%s::jsonb,applied_at=clock_timestamp() WHERE id=%s::uuid",(confirmation_ref,encoded(result),preview_id))
        return result
    def deliver_post_commit(self,row):
        deliveries=[]
        for mutation,plan in zip(row['payload'],row['plans']):
            handler=getattr(self.planner(mutation),'post_commit',None)
            if handler is None:
                continue
            deliveries.append({
                'operation':mutation['operation'],
                'target_id':plan.get('target_id'),
                'delivery':handler(mutation,plan),
            })
        return deliveries

    def apply(self,request_key,preview_id,preview_digest,confirmed,confirmation_ref):
        if confirmed is not True: raise Rejected('confirmation_required')
        arguments={'preview_id':preview_id,'preview_digest':preview_digest,'confirmed':confirmed,'confirmation_ref':confirmation_ref}
        with self.database.transaction() as tx:
            self.transaction_lock(tx,True)
            saved=self.receipt(tx,'apply',request_key,arguments)
            row=self.get_preview(tx,preview_id)
            if row['request_key']!=request_key: raise Rejected('reuse_preview_request_key')
            if saved is not None:
                result=saved
            else:
                result=self.apply_in_transaction(tx,preview_id,preview_digest,confirmation_ref)
                self.save_receipt(tx,'apply',request_key,arguments,result)
        deliveries=self.deliver_post_commit(row)
        if deliveries:
            result=dict(result)
            result['post_commit']=deliveries
        return result

    def _validate_managed_reconciliation_rule(self,tx,mutation,rule_key):
        if mutation['operation'] not in ('create_managed_entity','update_managed_entity'):
            return
        workspace_id=mutation.get('workspace_id')
        if not workspace_id:
            raise Rejected('managed_reconciliation_workspace_required')
        w=tx.one("SELECT settings FROM tm_config.workspaces WHERE id=%s::uuid AND instance_id=%s AND status='active'",(workspace_id,self.instance))
        profile=((w or {}).get('settings') or {}).get('analysis',{}).get('profile')
        if profile not in ('postproduction','rcc_settlement'):
            raise Rejected('managed_reconciliation_profile_required')
        entity_type=(mutation.get('changes') or {}).get('entity_type')
        if mutation['operation']=='update_managed_entity':
            current=tx.one("SELECT entity_type FROM tm_config.entities WHERE id=%s::uuid AND instance_id=%s AND workspace_id=%s::uuid",
                           (mutation.get('target_id'),self.instance,workspace_id))
            entity_type=(current or {}).get('entity_type')
        if profile=='rcc_settlement' and entity_type=='rcc-settlement-payment':
            raise Rejected('settlement_payment_requires_user_confirmation')
        row=tx.one("SELECT id,revision FROM tm_config.documents WHERE instance_id=%s AND workspace_id=%s::uuid AND kind='rule' AND status='active' AND body->>'decision_key'=%s ORDER BY revision DESC LIMIT 1",
                   (self.instance,workspace_id,rule_key))
        if not row:
            raise Rejected('active_workspace_rule_required')
        refs=(mutation.get('analysis') or {}).get('rule_refs') or []
        if not any(str(r.get('id'))==str(row['id']) and r.get('revision')==row['revision'] for r in refs if isinstance(r,dict)):
            raise Rejected('workspace_rule_reference_required')

    def apply_reconciliation(self,request_key,preview_id,preview_digest,finding_key,rule_key,rationale):
        """Apply a deterministic reconciliation preview under the owner's standing policy."""
        arguments={'preview_id':preview_id,'preview_digest':preview_digest,'finding_key':finding_key,
                   'rule_key':rule_key,'rationale':rationale}
        with self.database.transaction() as tx:
            self.transaction_lock(tx,True)
            saved=self.receipt(tx,'reconciliation_apply',request_key,arguments)
            if saved is not None:return saved
            row=self.get_preview(tx,preview_id)
            if row['request_key']!=request_key: raise Rejected('reuse_preview_request_key')
            for mutation in row['payload']:
                if mutation['operation'] not in RECONCILIATION_AUTO_OPS:
                    raise Rejected('reconciliation_operation_requires_user_confirmation')
                if any(item.get('kind')!='telegram' for item in mutation.get('evidence') or []):
                    raise Rejected('reconciliation_requires_telegram_evidence')
                analysis=mutation.get('analysis') or {}
                if (analysis.get('classification')!='deterministic'
                        or analysis.get('finding_key')!=finding_key
                        or analysis.get('rule_key')!=rule_key):
                    raise Rejected('reconciliation_analysis_binding_required')
                self._validate_managed_reconciliation_rule(tx,mutation,rule_key)
            confirmation_ref='reconciliation:'+finding_key
            result=self.apply_in_transaction(tx,preview_id,preview_digest,confirmation_ref)
            result=result|{'authorization':'reconciliation_standing_policy',
                           'finding_key':finding_key,'rule_key':rule_key,'rationale':rationale}
            self.save_receipt(tx,'reconciliation_apply',request_key,arguments,result)
            return result

    def submission(self,request_key):
        with self.database.transaction(read_only=True) as tx:
            rows=tx.all("SELECT operation,result,created_at FROM tm_v24.operations WHERE instance_id=%s AND request_key=%s AND operation IN ('apply','review_answer','reconciliation_apply')",(self.instance,request_key))
            return {'found':bool(rows),'receipts':rows}
