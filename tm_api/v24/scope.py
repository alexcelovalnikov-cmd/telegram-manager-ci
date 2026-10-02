"""All data access is constrained by the authenticated configured instance."""
from .common import Rejected, encoded

SCOPE = """g.reminder_list_instance_id=%s AND g.enabled AND g.monitoring_enabled"""
CHAT_SCOPE = """EXISTS (
 SELECT 1 FROM public.telegram_chat_groups g
 JOIN public.telegram_chat_group_members gm ON gm.group_id=g.id
 WHERE g.reminder_list_instance_id=%s AND g.enabled AND g.monitoring_enabled
 AND gm.chat_id=m.chat_id AND gm.enabled
 AND public.tm_group_chat_allowed_v14(g.id,m.chat_id))"""

def group(tx, instance, key, lock=False):
    row=tx.one('SELECT g.* FROM public.telegram_chat_groups g WHERE '+SCOPE+' AND g.group_key=%s'+(' FOR SHARE' if lock else ''),(instance,key))
    if not row:
        raise Rejected('group_not_authorized')
    return row

def task(tx, instance, ident, lock=False):
    try: ident=int(ident)
    except (TypeError,ValueError): raise Rejected('invalid_task_id') from None
    row=tx.one('SELECT t.* FROM public.tasks t JOIN public.telegram_chat_groups g ON g.id=t.context_group_id WHERE t.id=%s AND '+SCOPE+(' FOR UPDATE OF t' if lock else ''),(ident,instance))
    if not row:
        raise Rejected('entity_not_found')
    return row

def evidence(tx, instance, items, group_id=None, lock=False):
    if not items or len(items)>30:
        raise Rejected('evidence_required')
    for item in items:
        if item['kind']=='user':
            if len(item.get('confirmation_ref',''))<8 or not item.get('statement','').strip():
                raise Rejected('user_evidence_required')
            continue
        cid,mid=item['chat_id'],item['message_id']
        row=tx.one('SELECT m.chat_id,m.message_id FROM public.telegram_messages m WHERE m.chat_id=%s AND m.message_id=%s AND NOT m.is_deleted AND '+CHAT_SCOPE+(' FOR SHARE OF m' if lock else ''),(cid,mid,instance))
        if not row:
            raise Rejected('source_not_authorized_or_deleted')
        if group_id is not None and not tx.one('SELECT public.tm_group_chat_allowed_v14(%s,%s) AS ok',(group_id,cid))['ok']:
            raise Rejected('source_group_mismatch')
        if lock:
            tx.all('SELECT id FROM public.telegram_attachments WHERE chat_id=%s AND message_id=%s FOR SHARE',(cid,mid))
        token=tx.one('SELECT public.tm_content_token_v11(%s,%s) AS token',(cid,mid))['token']
        if token != item.get('content_token'):
            raise Rejected('source_revision_conflict')

def no_salary(tx, ident):
    if tx.one('SELECT 1 AS found FROM public.tm_salary_periods_v15 WHERE task_id=%s LIMIT 1',(ident,)) or tx.one('SELECT 1 AS found FROM public.tm_salary_series_v15 WHERE seed_task_id=%s LIMIT 1',(ident,)):
        raise Rejected('salary_requires_dedicated_operation')

def audit(tx, instance, operation, target, before, after, source):
    tx.execute('INSERT INTO tm_v24.audit(instance_id,operation,target_id,before_state,after_state,evidence) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)',(instance,operation,str(target) if target is not None else None,encoded(before),encoded(after),encoded(source)))
