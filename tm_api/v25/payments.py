"""Append-only net-work receipts, explicit document breakdowns and expectations."""
from copy import deepcopy
from datetime import datetime
from uuid import uuid5,NAMESPACE_URL
from decimal import Decimal
from tm_api.v24 import scope
from tm_api.v24.common import Rejected,strict_fields,check_revision,encoded
from . import finance,repository as config
OPS={'update_payment','create_payment','set_project_cost','create_payment_expectation','update_payment_expectation'}

class FinanceBusiness:
    def __init__(self,instance,managed):self.instance=instance;self.managed=managed
    def project(self,tx,ident,lock):
        t=scope.task(tx,self.instance,ident,lock);scope.no_salary(tx,t['id'])
        if t['record_kind']!='project' or t.get('project_archived_at'):raise Rejected('current_financial_project_required')
        return t
    def plan(self,tx,m,lock=False):
        op=m['operation'];p=m['changes']
        # Public finance mutations require direct user evidence. The internal
        # record_payment adapter is already gated by RecordPayment.confirmed.
        if m.get('_record_payment_confirmed') is not True:
            config.explicit(m)
        if op=='update_payment':return self.correction_plan(tx,m,lock)
        if op=='set_project_cost':
            value=finance.cost(p)
            current=self.project(tx,m['target_id'],lock)
            existing=current['project_data'].get('amount_rub')
            change={'label':current['project_data']['label']} if existing is not None and finance.money(str(existing))==finance.money(value['work_amount']) else {'amount_rub':value['work_amount']}
            working=deepcopy(m);working['operation']='update_project';working['changes']=change;working['_finance_cost']=value
            working,ctx=self.managed.scoped(tx,working)
            plan=self.managed.legacy.plan(tx,working,lock)
            # The inherited project planner validates money and renders titles but
            # normalizes amount_rub to two decimal places. Keep the explicit V25
            # work amount as the canonical project value (e.g. '30000', not
            # '30000.00') while finance_cost preserves the full breakdown.
            plan['after']['project_data']['amount_rub']=value['work_amount']
            plan['after']['project_data']['finance_cost']=value
            plan['operation']=op
            receipts=self.managed.receipts(tx,plan['target_id'])
            if receipts:
                computed=finance.balance(value['work_amount'],receipts)
                state=computed['payment_status']
                if state=='unknown' and plan['before']['payment_status']=='awaiting':state='awaiting'
                plan['after']['payment_status']=state
                plan['after']['payment_confirmed_at']='assigned_at_commit' if state=='paid' else None
                plan['after']['payment_evidence']={'user_confirmed':True,'source':'v25_cost_and_ledger','evidence':m['evidence'],
                    'removal_authorized':False,'reminder_projection':'hidden_paid' if state=='paid' else 'visible'}
                plan['ledger_basis']=receipts
            return self.managed.decorate(tx,m,plan,ctx)
        creating=op.startswith('create_');ident=str(uuid5(NAMESPACE_URL,'tm-finance:'+self.instance+':'+m['dedupe_key'])) if creating else m['target_id'];before=None
        if op=='create_payment':
            strict_fields(p,('kind','amounts','allocations','occurred_at','external_ref','method','counterparty'),('kind','amounts','allocations','occurred_at'))
            if p['kind'] not in ('received','refund'):raise Rejected('receipt_kind_must_be_received_or_refund')
            amounts=finance.cost(p['amounts']);work=finance.money(amounts['work_amount'])
            if work<=0:raise Rejected('positive_receipt_required')
            when=datetime.fromisoformat(p['occurred_at'].replace('Z','+00:00'))
            if when.tzinfo is None:raise Rejected('receipt_timezone_required')
            allocations=p['allocations']
            if not isinstance(allocations,list) or not 1<=len(allocations)<=100:raise Rejected('invalid_allocations')
            group_id=None;revisions={};total=Decimal(0);seen=set();projection=[]
            for a in allocations:
                strict_fields(a,('task_id','work_key','amount','expected_revision'),('task_id','amount','expected_revision'))
                task=self.project(tx,a['task_id'],lock);check_revision(task['updated_at'],a['expected_revision'])
                if group_id is not None and group_id!=task['context_group_id']:raise Rejected('cross_group_payment_not_supported')
                group_id=task['context_group_id'];scope.evidence(tx,self.instance,m['evidence'],group_id,lock)
                key=(task['id'],a.get('work_key','main'))
                if key in seen:raise Rejected('duplicate_payment_allocation')
                seen.add(key)
                if key[1]!='main' and not tx.one("SELECT id FROM public.tm_workstreams WHERE task_id=%s AND work_key=%s AND state<>'retired'",key):raise Rejected('unknown_workstream')
                amount=finance.money(a['amount'])
                if amount<=0:raise Rejected('positive_allocation_required')
                total+=amount;revisions[str(task['id'])]=task['updated_at']
                workspace=self.managed.locate(tx,{'target_id':str(task['id'])})
                if workspace:
                    ctx=config.context(tx,self.instance,workspace['id']);config.assert_context(ctx,m)
                receipts=self.managed.receipts(tx,task['id'])
                cost=(task['project_data'].get('finance_cost') or {}).get('work_amount',task['project_data'].get('amount_rub'))
                # One project can have multiple allocations; calculate them together below.
                if not any(x['task_id']==task['id'] for x in projection):
                    projection.append({'task_id':task['id'],'before':finance.balance(cost,receipts),'receipts':receipts,'cost':cost})
            if total>work:raise Rejected('allocations_exceed_net_work_receipt')
            for x in projection:
                new=[{'kind':p['kind'],'state':'confirmed','evidence_valid':True,'amount':a['amount']} for a in allocations if a['task_id']==x['task_id']]
                x['after']=finance.balance(x.pop('cost'),x['receipts']+new)
            for field in ('external_ref','method','counterparty'):
                if p.get(field) is not None and (not isinstance(p[field],str) or len(p[field])>500):raise Rejected('invalid_payment_text')
            if p.get('external_ref') and tx.one('SELECT id FROM public.tm_payment_events WHERE group_id=%s AND kind=%s AND external_ref=%s',(group_id,p['kind'],p['external_ref'])):raise Rejected('external_receipt_already_recorded')
            after={'id':ident,'kind':p['kind'],'amounts':amounts,'allocations':allocations,'group_id':group_id,
              'occurred_at':when.isoformat(),'external_ref':p.get('external_ref'),'method':p.get('method','unknown'),'counterparty':p.get('counterparty'),
              'balances':projection,'project_revisions':revisions,'expected_payments_counted':False}
        else:
            if creating:
                strict_fields(p,('task_id','amount','expected_at','expected_project_revision'),('task_id','amount','expected_project_revision'))
                t=self.project(tx,p['task_id'],lock);check_revision(t['updated_at'],p['expected_project_revision'])
                after={'id':ident,'instance_id':self.instance,'task_id':t['id'],'amount':str(finance.money(p['amount'])),'expected_at':p.get('expected_at'),'status':'open','revision':1}
            else:
                before=tx.one('SELECT * FROM tm_config.payment_expectations WHERE id=%s::uuid AND instance_id=%s'+(' FOR UPDATE' if lock else ''),(ident,self.instance))
                if not before:raise Rejected('payment_expectation_not_found')
                check_revision(before['revision'],m['expected_revision']);t=self.project(tx,before['task_id'],lock)
                strict_fields(p,('amount','expected_at','status'));after=deepcopy(before);after.update(p);after['revision']+=1
            scope.evidence(tx,self.instance,m['evidence'],t['context_group_id'],lock)
            if finance.money(after['amount'])<=0:raise Rejected('positive_expectation_required')
            if after['status'] not in ('open','fulfilled','cancelled'):raise Rejected('invalid_expectation_status')
            if after.get('expected_at'):
                from datetime import date
                date.fromisoformat(after['expected_at'])
            w=self.managed.locate(tx,{'target_id':str(t['id'])})
            if w:config.assert_context(config.context(tx,self.instance,w['id']),m)
        return {'entity':'payment' if op=='create_payment' else 'payment_expectation','operation':op,'target_id':ident,'before':before,'after':after}
    def execute(self,tx,m,plan):
        op=m['operation'];a=plan['after'];config.actor(tx,self.instance)
        if op=='set_project_cost':return self.managed.execute(tx,m,plan)
        if op=='update_payment':return self.correction_execute(tx,m,plan)
        if op=='create_payment':
            tx.execute("INSERT INTO public.tm_payment_events(id,request_key,group_id,kind,amount,currency,method,counterparty,external_ref,occurred_at,state,user_confirmed,evidence) VALUES(%s::uuid,%s,%s,%s,%s,'RUB',%s,%s,%s,%s::timestamptz,'confirmed',true,%s::jsonb)",
                (a['id'],'v25:'+a['id'],a['group_id'],a['kind'],a['amounts']['work_amount'],a['method'],a['counterparty'],a['external_ref'],a['occurred_at'],encoded(m['evidence'])))
            tx.execute('INSERT INTO tm_config.payment_details(payment_id,work_amount,tax_amount,document_total) VALUES(%s::uuid,%s,%s,%s)',(a['id'],a['amounts']['work_amount'],a['amounts']['tax_amount'],a['amounts']['document_total']))
            for allocation in a['allocations']:
                tx.execute('INSERT INTO public.tm_payment_allocations(payment_id,task_id,work_key,amount) VALUES(%s::uuid,%s,%s,%s)',(a['id'],allocation['task_id'],allocation.get('work_key','main'),allocation['amount']))
            for state in a['balances']:
                self.refresh_project(tx,state,m,a['id'])
            return {'entity':'payment','id':a['id'],'amounts':a['amounts'],'balances':[{'task_id':x['task_id'],**x['after']} for x in a['balances']], 'history_preserved':True,'removal_authorized':False}
        tx.execute('INSERT INTO tm_config.payment_expectations(id,instance_id,task_id,amount,expected_at,status,evidence,revision) VALUES(%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(id) DO UPDATE SET amount=EXCLUDED.amount,expected_at=EXCLUDED.expected_at,status=EXCLUDED.status,evidence=EXCLUDED.evidence,revision=EXCLUDED.revision,updated_at=clock_timestamp()',
            (a['id'],self.instance,a['task_id'],a['amount'],a.get('expected_at'),a['status'],encoded(m['evidence']),a['revision']))
        config.history(tx,self.instance,'payment_expectation',a['id'],a['revision'],a,m['evidence'],op)
        return {'entity':'payment_expectation','id':a['id'],'revision':a['revision'],'actual_receipt_created':False}

    def refresh_project(self,tx,state,m,receipt_id):
        ident=state['task_id'];new_status=state['after']['payment_status']
        old=tx.one('SELECT * FROM public.tasks WHERE id=%s FOR UPDATE',(ident,))
        if new_status=='unknown' and old['payment_status']=='awaiting':new_status='awaiting'
        previous_evidence=old.get('payment_evidence') or {}
        payment_owned_hidden=(
            old.get('payment_status')=='paid'
            and previous_evidence.get('reminder_projection')=='hidden_paid'
        )
        if payment_owned_hidden and new_status!='paid':
            tx.execute('UPDATE tm_config.reminder_bindings SET hidden=false WHERE task_id=%s',(ident,))
        ev={'user_confirmed':True,'source':'v25_net_work_ledger','payment_id':receipt_id,'evidence':m['evidence'],
            'removal_authorized':False,'reminder_projection':'hidden_paid' if new_status=='paid' else 'visible'}
        tx.execute("UPDATE public.tasks SET payment_status=%s,payment_confirmed_at=CASE WHEN %s='paid' THEN coalesce(payment_confirmed_at,clock_timestamp()) ELSE NULL END,payment_evidence=%s::jsonb,updated_at=clock_timestamp() WHERE id=%s",
            (new_status,new_status,encoded(ev),ident))
        w=self.managed.locate(tx,{'target_id':str(ident)})
        if w:
            current=tx.one('SELECT updated_at FROM public.tasks WHERE id=%s',(ident,))
            clone=deepcopy(m);clone.update(operation='update_project',target_id=str(ident),expected_revision=current['updated_at'],dedupe_key=None,changes={'label':old['project_data']['label']},workspace_id=w['id'])
            plan=self.managed.plan(tx,clone,lock=True);self.managed.execute(tx,clone,plan)
        tx.execute("INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence) VALUES(%s,'ledger_recalculated_v25','user_confirmed',%s::jsonb,%s::jsonb,%s::jsonb)",(ident,encoded(state['before']),encoded(state['after']),encoded(m['evidence'])))

    def correction_plan(self,tx,m,lock):
        plan=self.managed.legacy.payment_plan(tx,m,lock)
        original=plan['before']['event'];opposite='refund' if original['kind']=='received' else 'received'
        affected=sorted({x['task_id'] for x in plan['before']['allocations']+plan['after']['allocations']})
        projections=[]
        for ident in affected:
            t=self.project(tx,ident,lock)
            w=self.managed.locate(tx,{'target_id':str(ident)})
            if w:config.assert_context(config.context(tx,self.instance,w['id']),m)
            receipts=self.managed.receipts(tx,ident)
            total=(t['project_data'].get('finance_cost') or {}).get('work_amount',t['project_data'].get('amount_rub'))
            added=[{'kind':kind,'amount':a['amount'],'state':'confirmed','evidence_valid':True}
               for kind,allocations in ((opposite,plan['before']['allocations']),(original['kind'],plan['after']['allocations']))
               for a in allocations if a['task_id']==ident]
            for allocation in plan['after']['allocations']:
                if allocation['task_id']==ident and allocation.get('work_key','main')!='main':
                    if not tx.one("SELECT id FROM public.tm_workstreams WHERE task_id=%s AND work_key=%s AND state<>'retired'",(ident,allocation['work_key'])):raise Rejected('unknown_workstream')
            projections.append({'task_id':ident,'before':finance.balance(total,receipts),'after':finance.balance(total,receipts+added),'ledger_basis':receipts})
        plan['after']['balances']=projections
        plan['after']['document_breakdown']='original_retained; replacement_tax_unknown; all_amounts_are_net_work'
        return plan

    def correction_execute(self,tx,m,plan):
        original=plan['before']['event'];offset_id=str(uuid5(NAMESPACE_URL,'tm-offset:'+original['id']))
        replacement_id=str(uuid5(NAMESPACE_URL,'tm-replacement:'+original['id']))
        opposite='refund' if original['kind']=='received' else 'received'
        for ident,kind,value,allocations in (
            (offset_id,opposite,original['amount'],plan['before']['allocations']),
            (replacement_id,original['kind'],plan['after']['amount'],plan['after']['allocations'])):
            tx.execute("INSERT INTO public.tm_payment_events(id,request_key,group_id,kind,amount,currency,method,counterparty,occurred_at,state,user_confirmed,evidence) VALUES(%s::uuid,%s,%s,%s,%s,'RUB',%s,%s,clock_timestamp(),'confirmed',true,%s::jsonb)",
                (ident,'v25-correction:'+ident,original['group_id'],kind,value,original.get('method') or 'unknown',original.get('counterparty'),encoded(m['evidence'])))
            for a in allocations:
                tx.execute('INSERT INTO public.tm_payment_allocations(payment_id,task_id,work_key,amount) VALUES(%s::uuid,%s,%s,%s)',(ident,a['task_id'],a.get('work_key','main'),a['amount']))
        tx.execute('INSERT INTO tm_config.payment_details(payment_id,work_amount) VALUES(%s::uuid,%s)',(replacement_id,plan['after']['amount']))
        tx.execute('INSERT INTO tm_v24.payment_corrections(original_id,offset_id,replacement_id,reason) VALUES(%s::uuid,%s::uuid,%s::uuid,%s)',(original['id'],offset_id,replacement_id,plan['after']['reason']))
        for state in plan['after']['balances']:self.refresh_project(tx,state,m,replacement_id)
        return {'entity':'payment','id':replacement_id,'original_id':original['id'],'offset_id':offset_id,'replacement_id':replacement_id,'history_preserved':True,'project_payment_status_changed':True,'removal_authorized':False}
