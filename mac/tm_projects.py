"""Projects/payments for Telegram Manager V18.

Native Reminder flags are deliberately unsupported. Payment waiting state is
rendered in the project title as ``⚡️`` with an optional exact MM.DD expected
payment date. Work completion and payment state remain independent.
"""
import calendar,re,unicodedata
from decimal import Decimal,InvalidOperation

PROFILE='projects_payments'
NONE_DUE={'kind':'none'}
PAY_MARK='⚡️'
PAYMENT_SUFFIX_RE=re.compile(r'\s*⚡[\ufe0e\ufe0f]?(?:\s+(\d{2}\.\d{2}))?\s*$')
MUTABLE_FIELDS={'label','date_mmdd','date_iso','amount_rub','work_status','payment_status','expected_payment_mmdd'}

class ProjectError(RuntimeError):pass

def norm_id(value):return (value or '').strip().lower().removeprefix('x-apple-reminder://')

def amount(value):
    if value is None:return None
    if isinstance(value,bool):raise ProjectError('invalid_amount')
    try:n=Decimal(str(value).strip().replace(',','.'))
    except (InvalidOperation,ValueError):raise ProjectError('invalid_amount')
    if not n.is_finite() or n<0 or n>Decimal('1000000000') or n.quantize(Decimal('0.01'))!=n:raise ProjectError('invalid_amount')
    return format(n.quantize(Decimal('0.01')),'f')

def mmdd(value):
    if value is None:return None
    if not isinstance(value,str) or not re.fullmatch(r'\d{2}\.\d{2}',value):raise ProjectError('invalid_month_day')
    m,d=map(int,value.split('.'))
    if m not in range(1,13) or d not in range(1,calendar.monthrange(2000,m)[1]+1):raise ProjectError('invalid_month_day')
    return value

def strip_payment_suffix(title):
    return PAYMENT_SUFFIX_RE.sub('',str(title or '')).strip()

def payment_suffix(status,expected=None):
    if status not in ('awaiting','partial'):return ''
    expected=mmdd(expected) if expected else None
    return ' '+PAY_MARK+(' '+expected if expected else '')

def display_title(title,status,expected=None):
    return strip_payment_suffix(title)+payment_suffix(status,expected)

def render_title(data):
    label=data.get('label')
    if not isinstance(label,str) or not label.strip() or len(label)>350 or any(ord(c)<32 for c in label):raise ProjectError('invalid_project_label')
    title=strip_payment_suffix(label.strip())
    date=mmdd(data.get('date_mmdd'))
    if date:title=date+' '+title
    components=data.get('amount_breakdown') or {}
    if components.get('raw'):
        from tm_finance import amount_breakdown
        parsed=amount_breakdown('Project - '+components['raw'])
        if parsed['complete'] and parsed['total']==amount(data.get('amount_rub')):
            return display_title(title+' - '+parsed['raw'],data.get('payment_status'),data.get('expected_payment_mmdd'))
    n=amount(data.get('amount_rub'))
    if n is not None:
        x=Decimal(n)
        if x>=1000:
            raw=format(x/1000,'f');part=raw.rstrip('0').rstrip('.') if '.' in raw else raw;title+=' - '+part.replace('.',',')+'к'
        else:
            raw=format(x,'f');part=raw.rstrip('0').rstrip('.') if '.' in raw else raw;title+=' - '+part.replace('.',',')+'₽'
    return display_title(title,data.get('payment_status'),data.get('expected_payment_mmdd'))

def parse_title(title):
    if not isinstance(title,str):raise ProjectError('invalid_title')
    original=title.strip();label=original
    expected=None;awaiting=False
    m=PAYMENT_SUFFIX_RE.search(label)
    if m:
        awaiting=True
        if m.group(1):expected=mmdd(m.group(1))
        label=label[:m.start()].strip()
    result={'label':label,'date_mmdd':None,'date_iso':None,'amount_rub':None,'payment_status':'awaiting' if awaiting else None,'expected_payment_mmdd':expected}
    m=re.match(r'^(\d{2}\.\d{2})\s+(.+)$',label)
    if m:
        try:result['date_mmdd']=mmdd(m[1]);label=m[2]
        except ProjectError:pass
    m=re.search(r'\s+[-–—]\s*(\d+(?:[.,]\d{1,3})?)\s*([кkКK]|₽|руб\.?|р\.)\s*$',label)
    if m:
        n=Decimal(m[1].replace(',','.'))*(1000 if m[2].lower() in ('к','k') else 1)
        try:result['amount_rub']=amount(n);label=label[:m.start()].strip()
        except ProjectError:pass
    from tm_finance import amount_breakdown
    components=amount_breakdown(original)
    suffix=re.search(r'\s[-–—]\s+([^\n]+)$',label)
    if suffix and components['terms'] and all(t['state']!='unparsed' for t in components['terms']):
        label=label[:suffix.start()].strip()
        result['amount_rub']=components['total']
        result['amount_breakdown']=components
    result['label']=label
    return result

def label_key(value):
    s=unicodedata.normalize('NFKC',str(value)).casefold().replace('ё','е')
    return ' '.join(re.findall(r'[\w]+',s))

def possible_duplicates(data,projects):
    label=label_key(data.get('label',''));tokens=set(label.split());out=[]
    for t in projects:
        pd=t.get('project_data') or parse_title(t.get('title') or '')
        other=label_key(pd.get('label',''));ot=set(other.split())
        if label and (label==other or (len(tokens&ot)>=2 and len(tokens&ot)/max(len(tokens|ot),1)>=0.5)):out.append(t['id'])
    return out

def apple_state(row):
    if not row.get('id') or not row.get('fingerprint') or type(row.get('completed')) is not bool:raise ProjectError('incomplete_apple_snapshot')
    return {'title':row.get('title') or '','notes':row.get('notes') or '','completed':row['completed'],'due_spec':row.get('due_spec') or NONE_DUE,'url':row.get('url') or '','fingerprint':row['fingerprint']}

def desired_state(task):
    data=task.get('project_data') or {}
    from tm_payment_window import project_display
    title=project_display(task, task.get('title') or '')
    if data.get('display_protocol')=='v18_payment_window' and task.get('description'):
        raise ProjectError('project_notes_disabled_store_details_on_server')
    return {'title':title,'notes':task.get('description') or '','completed':task.get('status')=='completed'}

def merge_states(task,observed):
    """Merge editable content; payment decoration is not a financial input.

    Compare title bodies independently, then append the authoritative server
    suffix. Removing a marker on the Mac cannot confirm payment or reset debt.
    """
    if (task.get('project_data') or {}).get('display_protocol')=='v18_payment_window' and observed.get('notes'):
        raise ProjectError('project_notes_disabled_manual_note_preserved_for_review')
    base=task.get('project_sync_snapshot')
    if not isinstance(base,dict) or not base:raise ProjectError('project_snapshot_not_initialized')
    desired=desired_state(task);merged={};conflict=[]
    for key in ('title','notes','completed'):
        b,d,a=base.get(key),desired.get(key),observed.get(key)
        if key=='title':
            b,d,a=(strip_payment_suffix(x) for x in (b,d,a))
        if a!=b and d!=b and a!=d:conflict.append(key)
        else:merged[key]=a if a!=b else d
    if conflict:raise ProjectError('concurrent_project_edit:'+','.join(conflict))
    from tm_payment_window import project_display
    merged['title']=project_display(task, merged['title'])
    if (observed.get('due_spec') or NONE_DUE).get('kind')!='none':raise ProjectError('existing_due_date_requires_confirmation')
    return merged

def eventkit_patch(merged,observed):
    return {k:merged[k] for k in ('title','notes','completed') if merged[k]!=observed[k]}

def import_record(row):
    state=apple_state(row);data=parse_title(state['title']);data['work_status']='delivered' if state['completed'] else 'planned'
    return {'apple_reminder_id':row['id'],'title':display_title(state['title'],data.get('payment_status'),data.get('expected_payment_mmdd')),'description':state['notes'],'completed':state['completed'],'project_data':data,'snapshot':state,'completion_date':row.get('completed_at') or row.get('completion_date')}
