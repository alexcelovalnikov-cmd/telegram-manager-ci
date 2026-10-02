"""Calendar intents for the same preview/apply transaction as projects and tasks."""
from uuid import uuid5, NAMESPACE_URL
from tm_api.v24.common import Rejected,check_revision,strict_fields,encoded
from tm_api.v24.models import Event
from . import repository as repo,codec

class CalendarBusiness:
    def __init__(self,instance): self.instance=instance
    def plan(self,tx,m,lock=False):
        # All calendar writers (including credential/ACL administration) use this lock.
        repo.lock(tx,write=lock);name=repo.api_user(tx,self.instance)
        op=m['operation'];patch=m['changes'];before=None;extra={}
        ident=str(uuid5(NAMESPACE_URL,'tm-calendar:'+self.instance+':'+m['dedupe_key'])) if op.startswith('create_') else m['target_id']
        if op=='create_calendar':
            strict_fields(patch,('title','timezone'),('title',))
            from zoneinfo import ZoneInfo
            zone=patch.get('timezone','Asia/Yekaterinburg');ZoneInfo(zone)
            if not isinstance(patch['title'],str) or not 1<=len(patch['title'])<=200: raise Rejected('invalid_calendar_title')
            after={'id':ident,'title':patch['title'],'timezone':zone,'owner':name}
        elif op in ('update_calendar','set_calendar_member'):
            cid=m['calendar_id'] if op=='set_calendar_member' else ident
            if not cid: raise Rejected('calendar_id_required')
            calendar=repo.access(tx,name,cid,manage=True)
            if op=='update_calendar':
                check_revision(calendar['revision'],m['expected_revision'])
                strict_fields(patch,('title','timezone'))
                before={k:calendar[k] for k in ('id','title','timezone','revision')};after=before|patch
                if not isinstance(after['title'],str) or not 1<=len(after['title'])<=200: raise Rejected('invalid_calendar_title')
                from zoneinfo import ZoneInfo
                ZoneInfo(after['timezone'])
            else:
                repo.username(ident);strict_fields(patch,('role','status'),('role','status'))
                if patch['role'] not in ('owner','editor','viewer') or patch['status'] not in ('active','suspended','revoked'): raise Rejected('invalid_membership')
                if ident==name: raise Rejected('cannot_change_own_owner_access')
                if not tx.one('SELECT username FROM tm_calendar.users WHERE username=%s',(ident,)): raise Rejected('create_separate_account_first')
                before=tx.one('SELECT * FROM tm_calendar.members WHERE calendar_id=%s::uuid AND username=%s',(cid,ident))
                check_revision(before['revision'] if before else 0,m['expected_revision'])
                after={'calendar_id':cid,'username':ident,**patch}
        elif op in ('create_calendar_event','update_calendar_event','move_calendar_event','delete_calendar_event'):
            if op=='create_calendar_event':
                if not m.get('calendar_id'): raise Rejected('calendar_id_required')
                calendar=repo.access(tx,name,m['calendar_id'],write=True)
                if calendar.get('component_type','VEVENT')!='VEVENT':raise Rejected('use_reminder_operation_for_todo')
                uid=ident+'@telegram-manager';fields=Event.model_validate(patch).model_dump()
                text=codec.encode(fields,uid)
                after={'id':ident,'calendar_id':calendar['id'],'caldav_uid':uid,'fields':fields,'href':ident+'.ics'}
            else:
                row=repo.event(tx,name,ident,write=True)
                if row.get('component_type','VEVENT')!='VEVENT':raise Rejected('use_reminder_operation_for_todo')
                if op!='move_calendar_event' and m.get('calendar_id') not in (None,row['calendar_id']): raise Rejected('calendar_mismatch')
                check_revision(row['revision'],m['expected_revision'])
                before=repo.event_public(row)
                if op=='delete_calendar_event':
                    strict_fields(patch,());after={'id':ident,'deleted':True};text=None
                else:
                    strict_fields(patch,Event.model_fields.keys())
                    fields=Event.model_validate(row['fields']|patch).model_dump()
                    text=codec.encode(fields,row['caldav_uid'],row['icalendar'],changed=set(patch))
                    destination=row['calendar_id']
                    if op=='move_calendar_event':
                        if not m.get('calendar_id'):raise Rejected('calendar_id_required')
                        calendar=repo.access(tx,name,m['calendar_id'],write=True)
                        if calendar.get('component_type','VEVENT')!='VEVENT':raise Rejected('use_reminder_operation_for_todo')
                        if str(calendar['id'])==str(row['calendar_id']):raise Rejected('calendar_move_same_calendar')
                        destination=calendar['id']
                    after={'id':ident,'calendar_id':destination,'caldav_uid':row['caldav_uid'],'href':row['href'],'fields':fields}
            extra={'_icalendar':text}
        else: raise Rejected('unknown_calendar_operation')
        return {'entity':'calendar','operation':op,'target_id':ident,'before':before,'after':after,**extra}

    def execute(self,tx,m,plan):
        name=repo.api_user(tx,self.instance);op=m['operation'];a=plan['after'];ident=plan['target_id']
        if op=='create_calendar':
            result=repo.create(tx,name,a['title'],a['timezone'],ident)
        elif op=='update_calendar':
            c=repo.access(tx,name,ident,manage=True)
            props=c['props']|{'D:displayname':a['title']}
            result=tx.one('UPDATE tm_calendar.calendars SET title=%s,timezone=%s,props=%s::jsonb,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING *',(a['title'],a['timezone'],encoded(props),ident))
            repo.calendar_audit(tx,name,op,ident,plan['before'],result)
        elif op=='set_calendar_member':
            result=repo.member(tx,name,a['calendar_id'],ident,a['role'],a['status'],m['expected_revision'])
        elif op=='delete_calendar_event':
            result=repo.delete(tx,name,ident,m['expected_revision'])
        elif op=='move_calendar_event':
            result=repo.event_public(repo.move_event(tx,name,ident,a['calendar_id'],plan['_icalendar'],
                m['expected_revision'],source='telegram_manager'))
        else:
            result=repo.event_public(repo.put(tx,name,a['calendar_id'],a['href'],plan['_icalendar'],
                expected_revision=m.get('expected_revision'),ident=ident,source='telegram_manager'))
        return {'entity':'calendar','operation':op,'record':result,'sync_state':'consistent','device_delivery':'client_sync_pending'}
