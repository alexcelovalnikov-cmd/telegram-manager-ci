"""Work value, document tax, receipts and promises are separate data.

No tax rate is assumed. Receipt sums never come from parsed display titles.
Amounts use exact decimal strings in the external API.
"""
from decimal import Decimal,InvalidOperation
from tm_api.v24.common import Rejected,strict_fields

def money(value):
    if not isinstance(value,str):raise Rejected('decimal_string_required')
    try:d=Decimal(value)
    except InvalidOperation:raise Rejected('invalid_money') from None
    if not d.is_finite() or d<0 or d>Decimal('1000000000') or d!=d.quantize(Decimal('.01')):
        raise Rejected('invalid_money')
    return d

def cost(data):
    strict_fields(data,('work_amount','tax_amount','document_total'),('work_amount',))
    work=money(data['work_amount']);tax=money(data['tax_amount']) if data.get('tax_amount') is not None else None
    gross=money(data['document_total']) if data.get('document_total') is not None else None
    if tax is not None and gross is not None and work+tax!=gross:raise Rejected('document_total_mismatch')
    if gross is not None and gross<work:raise Rejected('document_total_below_work')
    return {'work_amount':str(work),'tax_amount':str(tax) if tax is not None else None,'document_total':str(gross) if gross is not None else None}

def balance(work_amount,receipts):
    total=money(work_amount) if work_amount is not None else None
    received=Decimal(0)
    for r in receipts:
        if r.get('state')!='confirmed' or r.get('evidence_valid') is not True:continue
        n=money(r['amount'])
        if r['kind']=='received':received+=n
        elif r['kind']=='refund':received-=n
        # Expected / promised payments deliberately contribute nothing.
    remaining=max(total-received,Decimal(0)) if total is not None else None
    status='unknown' if total is None or received<=0 else 'paid' if received>=total else 'partial'
    return {'work_amount':str(total) if total is not None else None,'received_net':str(received),
            'remaining':str(remaining) if remaining is not None else None,
            'overpayment':str(max(received-total,Decimal(0))) if total is not None else None,
            'payment_status':status,'negative_receipts_require_review':received<0}

def compact(value,formatting):
    if value is None:return ''
    n=Decimal(str(value));large=abs(n)>=1000;v=n/1000 if large else n
    s=format(v,'f').rstrip('0').rstrip('.') if '.' in format(v,'f') else format(v,'f')
    return s.replace('.',formatting['decimal_separator'])+(formatting['thousands_suffix'] if large else formatting['currency_suffix'])

def display(finance,formatting):
    total=compact(finance['work_amount'],formatting)
    if finance['payment_status']=='partial':
        return formatting['received_marker']+compact(finance['received_net'],formatting)+formatting['partial_separator']+compact(finance['remaining'],formatting)
    # The received marker is a partial-payment affordance only. Fully paid
    # projects are removed from the Reminder projection and retain a plain
    # historical amount in canonical server state.
    if finance['payment_status']=='paid':
        return total
    return total
