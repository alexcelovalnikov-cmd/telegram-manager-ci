"""Canonical RCC client-settlement calculations.

The settlement ledger is deliberately separate from the personal project ledger:
client_settlement says money is part of Alexandra/RCC -> Alexey reporting;
personal_income says the amount belongs in Alexey's Projects reminder value.
"""
from decimal import Decimal, InvalidOperation
from tm_api.v24.common import Rejected

PROJECT_TYPE='rcc-settlement-project'
ITEM_TYPE='rcc-settlement-item'
PAYMENT_TYPE='rcc-settlement-payment'
ENTITY_TYPES=(PROJECT_TYPE,ITEM_TYPE,PAYMENT_TYPE)

def money(value):
    if not isinstance(value,str):
        raise Rejected('settlement_money_requires_decimal_string')
    try:
        n=Decimal(value)
    except InvalidOperation:
        raise Rejected('invalid_settlement_money') from None
    if not n.is_finite() or n<0 or n>Decimal('1000000000') or n!=n.quantize(Decimal('.01')):
        raise Rejected('invalid_settlement_money')
    return n

def rubles(value):
    n=Decimal(value)
    whole=n==n.to_integral()
    text=f'{int(n):,}'.replace(',',' ') if whole else f'{n:,.2f}'.replace(',',' ').replace('.',',')
    return text+' ₽'

def _payment_display(payments,total_received,item_amount):
    confirmed=[p for p in payments if p['state']=='confirmed']
    if not confirmed:
        return ''
    if len(confirmed)==1 and confirmed[0]['kind']=='received' and confirmed[0]['amount']==item_amount:
        return confirmed[0]['payment_type']
    parts=[]
    for p in confirmed:
        prefix='−' if p['kind']=='refund' else ''
        label=(prefix+rubles(p['amount'])+' '+p['payment_type']).strip()
        parts.append(label)
    return ' + '.join(parts)

def build_summary(rows):
    projects={}
    items={}
    payments=[]
    for row in rows:
        if row.get('status')!='active':
            continue
        data=row.get('data') or {}
        kind=row.get('entity_type')
        base={'id':str(row['id']),'revision':row['revision'],'data':data}
        if kind==PROJECT_TYPE:
            title=data.get('title')
            if not isinstance(title,str) or not title.strip():
                raise Rejected('settlement_project_title_required')
            personal_id=data.get('personal_project_id')
            if personal_id is not None and (type(personal_id) is not int or personal_id<=0):
                raise Rejected('invalid_personal_project_id')
            projects[base['id']]={'id':base['id'],'revision':row['revision'],'title':title.strip(),
                'date_iso':data.get('date_iso'),'personal_project_id':personal_id,'items':[]}
        elif kind==ITEM_TYPE:
            parent=str(data.get('parent_id') or '')
            title=data.get('title')
            if not parent or not isinstance(title,str) or not title.strip():
                raise Rejected('settlement_item_parent_and_title_required')
            if type(data.get('client_settlement')) is not bool or type(data.get('personal_income')) is not bool:
                raise Rejected('settlement_item_scope_flags_required')
            amount=money(data.get('amount_rub'))
            items[base['id']]={'id':base['id'],'revision':row['revision'],'project_id':parent,
                'title':title.strip(),'category':data.get('category') or 'other','amount':amount,
                'client_settlement':data['client_settlement'],'personal_income':data['personal_income'],
                'pass_through':bool(data.get('pass_through',not data['personal_income'])),
                'recipient':data.get('recipient') or None,'performer':data.get('performer') or None,
                'order':data.get('order') if type(data.get('order')) is int else 1000,
                'state':data.get('state') or 'active','payments':[]}
        elif kind==PAYMENT_TYPE:
            parent=str(data.get('parent_id') or '')
            if not parent:
                raise Rejected('settlement_payment_parent_required')
            amount=money(data.get('amount_rub'))
            ptype=data.get('payment_type')
            if not isinstance(ptype,str) or not ptype.strip():
                raise Rejected('settlement_payment_type_required')
            kind_value=data.get('kind','received')
            state=data.get('state','confirmed')
            if kind_value not in ('received','refund') or state not in ('confirmed','cancelled'):
                raise Rejected('invalid_settlement_payment_state')
            payments.append({'id':base['id'],'revision':row['revision'],'item_id':parent,
                'amount':amount,'payment_type':ptype.strip(),'kind':kind_value,'state':state,
                'occurred_at':data.get('occurred_at')})

    for p in payments:
        if p['item_id'] in items:
            items[p['item_id']]['payments'].append(p)
    for item in items.values():
        if item['project_id'] not in projects or item['state']=='cancelled':
            continue
        item['payments'].sort(key=lambda p:(p.get('occurred_at') or '',p['id']))
        received=Decimal(0)
        for payment in item['payments']:
            if payment['state']!='confirmed':
                continue
            received += payment['amount'] if payment['kind']=='received' else -payment['amount']
        received=max(received,Decimal(0))
        remaining=max(item['amount']-received,Decimal(0))
        item['received']=received
        item['remaining']=remaining
        item['paid']=remaining==0
        item['payment_display']=_payment_display(item['payments'],received,item['amount'])
        projects[item['project_id']]['items'].append(item)

    result=[]
    for project in projects.values():
        project['items'].sort(key=lambda x:(x['order'],x['title'].casefold(),x['id']))
        settlement=[x for x in project['items'] if x['client_settlement']]
        personal=[x for x in project['items'] if x['personal_income']]
        project['settlement_total']=sum((x['amount'] for x in settlement),Decimal(0))
        project['settlement_received']=sum((min(x['received'],x['amount']) for x in settlement),Decimal(0))
        project['settlement_remaining']=sum((x['remaining'] for x in settlement),Decimal(0))
        project['personal_total']=sum((x['amount'] for x in personal),Decimal(0))
        project['paid']=project['settlement_remaining']==0 if settlement else False
        result.append(project)
    result.sort(key=lambda p:(p.get('date_iso') or '9999-12-31',p['title'].casefold(),p['id']))

    def public_item(item):
        return {k:v for k,v in item.items() if k not in ('amount','received','remaining','payments')} | {
            'amount_rub':str(item['amount']),'received_rub':str(item['received']),
            'remaining_rub':str(item['remaining']),
            'payments':[{**{k:v for k,v in p.items() if k!='amount'},'amount_rub':str(p['amount'])} for p in item['payments']]}

    public=[]
    for p in result:
        public.append({k:v for k,v in p.items() if k not in ('items','settlement_total','settlement_received','settlement_remaining','personal_total')} | {
            'settlement_total_rub':str(p['settlement_total']),
            'settlement_received_rub':str(p['settlement_received']),
            'settlement_remaining_rub':str(p['settlement_remaining']),
            'personal_total_rub':str(p['personal_total']),
            'items':[public_item(x) for x in p['items']]})
    return {'projects':public,
            'settlement_total_rub':str(sum((p['settlement_total'] for p in result),Decimal(0))),
            'received_total_rub':str(sum((p['settlement_received'] for p in result),Decimal(0))),
            'remaining_total_rub':str(sum((p['settlement_remaining'] for p in result),Decimal(0)))}

def read_summary(tx,instance,workspace_id):
    w=tx.one("SELECT id,workspace_key,name,settings,status FROM tm_config.workspaces WHERE id=%s::uuid AND instance_id=%s",
             (workspace_id,instance))
    if not w or w['status']!='active':
        raise Rejected('active_workspace_required')
    if ((w.get('settings') or {}).get('analysis') or {}).get('profile')!='rcc_settlement':
        raise Rejected('rcc_settlement_profile_required')
    rows=tx.all("SELECT id,entity_type,data,status,revision FROM tm_config.entities WHERE instance_id=%s AND workspace_id=%s::uuid AND entity_type=ANY(%s::text[]) ORDER BY id",
                (instance,workspace_id,list(ENTITY_TYPES)))
    result=build_summary(rows)
    result['workspace_id']=str(w['id'])
    result['workspace_key']=w['workspace_key']
    return result
