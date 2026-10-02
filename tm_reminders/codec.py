"""Lossless VTODO resource updates, using the real icalendar library.

Unknown properties/alarms and recurrence exceptions are kept. Time/recurrence
edits on exception series are rejected rather than silently corrupting them.
"""
from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo
from tm_api.v24.common import Rejected
from tm_calendar.codec import etag,zone_component,MAX_ICS_BYTES
from .models import Reminder
STATUSES={'open':'NEEDS-ACTION','in_progress':'IN-PROCESS','completed':'COMPLETED','cancelled':'CANCELLED'}

def decode(text,default_timezone='Asia/Yekaterinburg'):
    from icalendar import Calendar
    if not isinstance(text,str) or len(text.encode())>MAX_ICS_BYTES:raise Rejected('reminder_resource_too_large')
    cal=Calendar.from_ical(text)
    if cal.name!='VCALENDAR' or cal.get('METHOD'):raise Rejected('invalid_reminder_resource')
    todos=[c for c in cal.subcomponents if c.name!='VTIMEZONE']
    if not todos or len(todos)>1000 or any(c.name!='VTODO' for c in todos):raise Rejected('only_todo_resources_supported')
    uids={str(c.get('UID','')) for c in todos}
    if len(uids)!=1 or not next(iter(uids)) or len(next(iter(uids)))>256:raise Rejected('one_stable_uid_required')
    masters=[c for c in todos if not c.get('RECURRENCE-ID')]
    if len(masters)!=1:raise Rejected('single_todo_master_required')
    t=masters[0];due=t.decoded('DUE') if 'DUE' in t else None
    all_day=isinstance(due,date) and not isinstance(due,datetime)
    zone=str(t['DUE'].params.get('TZID') or default_timezone) if due else default_timezone
    try:ZoneInfo(zone)
    except (ValueError,KeyError):zone=default_timezone
    if isinstance(due,datetime) and due.tzinfo is None:due=due.replace(tzinfo=ZoneInfo(zone))
    categories=t.get('CATEGORIES');values=[str(x) for x in categories.cats if str(x)] if categories and hasattr(categories,'cats') else []
    status={v:k for k,v in STATUSES.items()}.get(str(t.get('STATUS','NEEDS-ACTION')).upper())
    if status is None:raise Rejected('invalid_todo_status')
    recurrence=t['RRULE'].to_ical().decode() if 'RRULE' in t else None
    if recurrence:
        rule=t['RRULE'];freq=str(rule.get('FREQ',[''])[0])
        if freq not in ('DAILY','WEEKLY','MONTHLY','YEARLY'):raise Rejected('unsupported_todo_recurrence_frequency')
        if int(rule.get('INTERVAL',[1])[0])<1 or int(rule.get('COUNT',[1])[0])<1:raise Rejected('invalid_todo_recurrence')
    fields=Reminder(title=str(t.get('SUMMARY','')) or 'Без названия',description=str(t.get('DESCRIPTION','')),
        due_at=due.isoformat() if due else None,all_day=all_day,timezone=zone,status=status,
        categories=values,priority=int(t.get('PRIORITY',0)),recurrence=recurrence).model_dump()
    return {'caldav_uid':next(iter(uids)),'fields':fields,'etag':etag(text),'component_type':'VTODO'}

def encode(fields,uid,previous=None,changed=None):
    from icalendar import Calendar,Todo,Timezone,vRecur
    data=Reminder.model_validate(fields).model_dump();changed=set(data if changed is None else changed)
    if previous:
        cal=Calendar.from_ical(previous);todos=cal.walk('VTODO');masters=[t for t in todos if not t.get('RECURRENCE-ID')]
        if len(masters)!=1:raise Rejected('single_todo_master_required')
        t=masters[0]
        if str(t.get('UID'))!=uid:raise Rejected('reminder_uid_immutable')
        if len(todos)>1 and changed & {'due_at','all_day','timezone','recurrence'}:raise Rejected('edit_todo_series_exceptions_in_client')
    else:
        cal=Calendar();cal.add('VERSION','2.0');cal.add('PRODID','-//Telegram Manager//V25//RU')
        t=Todo();t.add('UID',uid);cal.add_component(t)
    def replace(key,value):
        t.pop(key,None)
        if value is not None:t.add(key,value)
    for field,key in [('title','SUMMARY'),('description','DESCRIPTION'),('priority','PRIORITY')]:
        if field in changed:replace(key,data[field])
    if 'categories' in changed:replace('CATEGORIES',data['categories'] if data['categories'] else None)
    if 'status' in changed:
        replace('STATUS',STATUSES[data['status']]);replace('PERCENT-COMPLETE',100 if data['status']=='completed' else 0)
        if data['status']=='completed':
            if 'COMPLETED' not in t:replace('COMPLETED',datetime.now(timezone.utc))
        else:t.pop('COMPLETED',None)
    if changed & {'due_at','all_day','timezone'}:
        due=data['due_at'];zone=ZoneInfo(data['timezone'])
        value=None if not due else date.fromisoformat(due) if data['all_day'] else datetime.fromisoformat(due.replace('Z','+00:00')).astimezone(zone)
        replace('DUE',value)
        # A VTODO cannot combine DUE and DURATION.
        t.pop('DURATION',None)
        if isinstance(value,datetime) and data['timezone']!='UTC' and not any(str(z.get('TZID'))==data['timezone'] for z in cal.walk('VTIMEZONE')):
            cal.add_component(Timezone.from_ical(zone_component(data['timezone'])))
    if 'recurrence' in changed:replace('RRULE',vRecur.from_ical(data['recurrence']) if data['recurrence'] else None)
    replace('SEQUENCE',int(t.get('SEQUENCE',0))+1);replace('DTSTAMP',datetime.now(timezone.utc));replace('LAST-MODIFIED',datetime.now(timezone.utc))
    text=cal.to_ical().decode();decode(text,data['timezone']);return text
