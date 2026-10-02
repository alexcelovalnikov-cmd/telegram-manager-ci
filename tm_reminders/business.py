from uuid import uuid5,NAMESPACE_URL
from tm_api.v24.common import Rejected,check_revision,strict_fields,encoded
from tm_calendar import repository as repo
from .models import Reminder
from . import codec
OPS={'create_reminder_list','update_reminder_list','delete_reminder_list','create_reminder','update_reminder','complete_reminder','delete_reminder','move_reminder','normalize_reminder_categories'}

class RemindersBusiness:
    def __init__(self,instance):self.instance=instance
    def plan(self,tx,m,lock=False):
        repo.lock(tx,write=lock);user=repo.api_user(tx,self.instance);op=m['operation'];p=m['changes'];before=None;extra={}
        ident=str(uuid5(NAMESPACE_URL,'tm-reminder:'+self.instance+':'+m['dedupe_key'])) if op.startswith('create_') else m['target_id']
        if op=='create_reminder_list':
            strict_fields(p,('title','timezone'),('title',))
            from zoneinfo import ZoneInfo
            ZoneInfo(p.get('timezone','Asia/Yekaterinburg'))
            if not isinstance(p['title'],str) or not 1<=len(p['title'])<=200:raise Rejected('invalid_list_title')
            after={'id':ident,'title':p['title'],'timezone':p.get('timezone','Asia/Yekaterinburg'),'component_type':'VTODO'}
        elif op in ('update_reminder_list','delete_reminder_list'):
            c=repo.access(tx,user,ident,manage=True);check_revision(c['revision'],m['expected_revision'])
            if c.get('component_type')!='VTODO':raise Rejected('not_reminder_list')
            before={k:c[k] for k in ('id','title','timezone','revision','component_type')}
            if op=='delete_reminder_list':
                strict_fields(p,())
                if tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL LIMIT 1',(ident,)):raise Rejected('nonempty_list_requires_explicit_migration')
                if tx.one("SELECT id FROM tm_config.workspaces WHERE settings->'reminder_list'->>'id'=%s AND status='active'",(ident,)):raise Rejected('list_is_workspace_destination')
                after={'id':ident,'deleted':True}
            else:
                strict_fields(p,('title',),('title',))
                if not isinstance(p['title'],str) or not 1<=len(p['title'])<=200:raise Rejected('invalid_list_title')
                after=before|p
        else:
            if op=='create_reminder':
                if not m.get('calendar_id'):raise Rejected('list_id_required_in_calendar_id')
                c=repo.access(tx,user,m['calendar_id'],write=True)
                fields=Reminder.model_validate(p).model_dump();uid=ident+'@telegram-manager';href=ident+'.ics';text=codec.encode(fields,uid)
            else:
                row=repo.event(tx,user,ident,write=True)
                if m.get('calendar_id') not in (None,row['calendar_id']):raise Rejected('reminder_list_mismatch')
                c=repo.access(tx,user,row['calendar_id'],write=True)
                if row.get('component_type')!='VTODO':raise Rejected('not_reminder')
                check_revision(row['revision'],m['expected_revision']);before=repo.event_public(row)
                bound=tx.one('SELECT task_id FROM tm_config.reminder_bindings WHERE resource_id=%s::uuid',(ident,))
                if bound and op!='normalize_reminder_categories':
                    raise Rejected('bound_task_use_task_or_project_mutation')
                fields=row['fields'];uid=row['caldav_uid'];href=row['href']
                if op=='delete_reminder':strict_fields(p,());text=None
                elif op=='normalize_reminder_categories':
                    strict_fields(p,('categories',),('categories',))
                    if p['categories']!=[]:raise Rejected('normalization_only_clears_categories')
                    if fields.get('categories')!=['']:raise Rejected('normalization_source_not_empty_category_artifact')
                    fields=Reminder.model_validate(fields|{'categories':[]}).model_dump()
                    text=codec.encode(fields,uid,row['icalendar'],changed={'categories'})
                elif op=='move_reminder':
                    strict_fields(p,('list_id',),('list_id',));destination=repo.access(tx,user,p['list_id'],write=True)
                    if destination.get('component_type')!='VTODO':raise Rejected('not_reminder_list')
                    if destination['id']==c['id']:raise Rejected('already_in_list')
                    if tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND (caldav_uid=%s OR href=%s)',(destination['id'],uid,href)):raise Rejected('destination_uid_exists')
                    text=row['icalendar'];extra['destination_revision']=destination['revision']
                else:
                    strict_fields(p,() if op=='complete_reminder' else Reminder.model_fields.keys())
                    changes={'status':'completed'} if op=='complete_reminder' else p
                    fields=Reminder.model_validate(fields|changes).model_dump();text=codec.encode(fields,uid,row['icalendar'],changed=set(changes))
            if c.get('component_type')!='VTODO':raise Rejected('not_reminder_list')
            after={'id':ident,'calendar_id':p['list_id'] if op=='move_reminder' else c['id'],'fields':fields,'caldav_uid':uid,'href':href,'deleted':op=='delete_reminder','component_type':'VTODO'}
            extra['_icalendar']=text
        return {'entity':'reminder_list' if 'list' in op else 'reminder','operation':op,'target_id':ident,'before':before,'after':after,**extra}
    def execute(self,tx,m,plan):
        user=repo.api_user(tx,self.instance);a=plan['after'];op=m['operation'];ident=plan['target_id']
        if op=='create_reminder_list':r=repo.create(tx,user,a['title'],a['timezone'],ident,component_type='VTODO')
        elif op=='update_reminder_list':
            c=repo.access(tx,user,ident,manage=True)
            r=tx.one('UPDATE tm_calendar.calendars SET title=%s,props=%s::jsonb,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING *',(a['title'],encoded(c['props']|{'D:displayname':a['title']}),ident))
        elif op=='delete_reminder_list':
            r=tx.one('UPDATE tm_calendar.calendars SET deleted_at=clock_timestamp(),revision=revision+1 WHERE id=%s::uuid RETURNING id,revision',(ident,))
        elif op=='delete_reminder':r=repo.delete(tx,user,ident,m['expected_revision'])
        elif op=='move_reminder':
            old=plan['before'];r=tx.one('UPDATE tm_calendar.events SET calendar_id=%s::uuid,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING *',(a['calendar_id'],user,ident))
            repo.changed(tx,old['calendar_id'],old['href'],True);repo.changed(tx,a['calendar_id'],a['href'],False)
        else:r=repo.put(tx,user,a['calendar_id'],a['href'],plan['_icalendar'],expected_revision=m.get('expected_revision'),ident=ident,source='telegram_manager')
        repo.calendar_audit(tx,user,op,ident,plan['before'],plan['after'])
        return {'entity':plan['entity'],'id':ident,'record':repo.event_public(r),'sync_state':'consistent','device_delivery':'client_sync_pending'}
