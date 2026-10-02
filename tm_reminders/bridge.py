"""Inbound edits keep the canonical task and its CalDAV projection atomic.

A changed financial display is a manual override, never a receipt or tax parser.
Deleting a financial reminder hides only its projection; it cannot mark it paid.
"""
from tm_api.v24.common import Rejected,encoded
from tm_api.v25.repository import actor

def binding(tx,resource_id):
    return tx.one('SELECT b.*,w.instance_id,w.status AS workspace_status FROM tm_config.reminder_bindings b JOIN tm_config.workspaces w ON w.id=b.workspace_id WHERE b.resource_id=%s::uuid',(resource_id,))

def incoming(tx,user,row,old):
    b=binding(tx,row['id'])
    if not b:return
    if b['workspace_status']!='active':raise Rejected('workspace_not_active')
    actor(tx,b['instance_id'])
    task=tx.one('SELECT * FROM public.tasks WHERE id=%s FOR UPDATE',(b['task_id'],))
    fields=row['fields'];status={'completed':'completed','cancelled':'cancelled','in_progress':'waiting'}.get(fields['status'],'open')
    if task['record_kind']=='project' and fields.get('due_at'):raise Rejected('project_date_is_not_reminder_due_date')
    if task['record_kind']=='project':
        # Keep the financial and project identity intact; completion != payment.
        data=dict(task['project_data']);data['work_status']='delivered' if status=='completed' else 'in_progress' if status=='waiting' else 'planned'
        if data.get('request_state') in ('pending','rejected') and status=='completed':raise Rejected('confirm_project_request_first')
        tx.execute("UPDATE public.tasks SET title=%s,status=%s,project_data=%s::jsonb,completed_at=CASE WHEN %s='completed' THEN coalesce(completed_at,clock_timestamp()) ELSE NULL END,cancelled_at=CASE WHEN %s='cancelled' THEN coalesce(cancelled_at,clock_timestamp()) ELSE NULL END,updated_at=clock_timestamp() WHERE id=%s",
            (fields['title'],status,encoded(data),status,status,task['id']))
    else:
        # Date-only VTODO uses the structured native date spec; no timezone shift.
        due=fields.get('due_at') if not fields['all_day'] else None
        due_spec={'kind':'none'} if not fields.get('due_at') else {'kind':'date','date':fields['due_at'],'timezone':fields['timezone']} if fields['all_day'] else None
        tx.execute("UPDATE public.tasks SET title=%s,description=%s,status=%s,due_at=%s::timestamptz,reminder_due_spec=%s::jsonb,tags=%s,completed_at=CASE WHEN %s='completed' THEN coalesce(completed_at,clock_timestamp()) ELSE NULL END,cancelled_at=CASE WHEN %s='cancelled' THEN coalesce(cancelled_at,clock_timestamp()) ELSE NULL END,updated_at=clock_timestamp() WHERE id=%s",
            (fields['title'],fields['description'],status,due,encoded(due_spec),[x for x in fields['categories'] if x],status,status,task['id']))
    if not old or fields['title']!=old['fields']['title']:
        tx.execute('UPDATE tm_config.reminder_bindings SET manual_title=%s WHERE task_id=%s',(fields['title'],task['id']))
    if not old or fields['description']!=old['fields']['description']:
        tx.execute('UPDATE tm_config.reminder_bindings SET manual_description=%s WHERE task_id=%s',(fields['description'],task['id']))

def incoming_delete(tx,user,row):
    b=binding(tx,row['id'])
    if not b:return
    actor(tx,b['instance_id'])
    tx.execute('UPDATE tm_config.reminder_bindings SET hidden=true WHERE task_id=%s',(b['task_id'],))
    tx.execute('UPDATE public.tasks SET reminder_active=false,updated_at=clock_timestamp() WHERE id=%s',(b['task_id'],))
