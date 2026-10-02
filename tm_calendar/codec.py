"""Lossless iCalendar container updates using icalendar (not a CalDAV implementation)."""
import hashlib
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
from tm_api.v24.common import Rejected
from tm_api.v24.models import Event

MAX_ICS_BYTES=500_000

def etag(text):
    return '"'+hashlib.sha256(text.encode()).hexdigest()+'"'

@lru_cache(maxsize=64)
def zone_component(zone):
    from icalendar import Timezone
    return Timezone.from_tzinfo(ZoneInfo(zone),tzid=zone,first_date=date(1970,1,1),last_date=date(2101,1,1)).to_ical()

def decode(text, default_timezone='Asia/Yekaterinburg'):
    from icalendar import Calendar
    if not isinstance(text,str) or len(text.encode())>MAX_ICS_BYTES:
        raise Rejected('calendar_resource_too_large')
    cal=Calendar.from_ical(text)
    if cal.name!='VCALENDAR' or cal.get('METHOD'):
        raise Rejected('invalid_calendar_resource')
    components=[c for c in cal.subcomponents if c.name!='VTIMEZONE']
    if not components or any(c.name!='VEVENT' for c in components) or len(components)>1000:
        raise Rejected('only_event_resources_supported')
    uids={str(c.get('UID','')) for c in components}
    if len(uids)!=1 or not next(iter(uids)) or len(next(iter(uids)))>256:
        raise Rejected('one_stable_uid_required')
    masters=[c for c in components if not c.get('RECURRENCE-ID')]
    if len(masters)>1: raise Rejected('multiple_event_masters')
    master=masters[0] if masters else components[0]
    if 'DTSTART' not in master: raise Rejected('event_start_required')
    a=master.decoded('DTSTART');all_day=isinstance(a,date) and not isinstance(a,datetime)
    z=master.decoded('DTEND') if 'DTEND' in master else a+(master.decoded('DURATION') if 'DURATION' in master else timedelta(days=1) if all_day else timedelta(0))
    zone=str(master['DTSTART'].params.get('TZID') or cal.get('X-WR-TIMEZONE') or default_timezone)
    try: ZoneInfo(zone)
    except (ValueError,KeyError): zone=default_timezone
    floating=False
    if not all_day:
        if not isinstance(z,datetime): raise Rejected('inconsistent_event_dates')
        floating=a.tzinfo is None
        if a.tzinfo is None: a=a.replace(tzinfo=ZoneInfo(zone))
        if z.tzinfo is None: z=z.replace(tzinfo=ZoneInfo(zone))
    if z<a or (all_day and z==a): raise Rejected('invalid_event_duration')
    for c in components:
        if c.get('RRULE'):
            rule=c['RRULE']
            freq=str(rule.get('FREQ',[''])[0])
            if freq not in ('DAILY','WEEKLY','MONTHLY','YEARLY'): raise Rejected('recurrence_frequency_not_supported')
            count=int(rule.get('COUNT',[1])[0]);interval=int(rule.get('INTERVAL',[1])[0])
            if not 1<=count<=10000: raise Rejected('invalid_recurrence_count')
            if not 1<=interval<=10000: raise Rejected('invalid_recurrence_interval')
    fields={'title':str(master.get('SUMMARY','')),'description':str(master.get('DESCRIPTION','')),
        'location':str(master.get('LOCATION','')),'start_at':a.isoformat(),'end_at':z.isoformat(),
        'timezone':zone,'all_day':all_day,'recurrence':master['RRULE'].to_ical().decode() if master.get('RRULE') else None,
        'status':str(master.get('STATUS','CONFIRMED')).lower()}
    if fields['status'] not in ('confirmed','tentative','cancelled'): raise Rejected('invalid_event_status')
    if len(fields['title'])>500 or len(fields['description'])>20000 or len(fields['location'])>2000: raise Rejected('event_text_too_large')
    return {'caldav_uid':next(iter(uids)),'fields':fields,'icalendar':text,'etag':etag(text),
            'has_master':bool(masters),'exception_count':len(components)-len(masters),'floating_time':floating}

def encode(fields, uid, previous=None, changed=None):
    from icalendar import Calendar, Event as IEvent, Timezone, vRecur
    data=Event.model_validate(fields).model_dump()
    changed=set(data if changed is None else changed)
    if previous:
        cal=Calendar.from_ical(previous);masters=[e for e in cal.walk('VEVENT') if not e.get('RECURRENCE-ID')]
        if len(masters)!=1: raise Rejected('masterless_series_requires_caldav_edit')
        component=masters[0]
        if len(cal.walk('VEVENT'))>1 and changed & {'start_at','end_at','all_day','recurrence','timezone'}:
            raise Rejected('edit_recurrence_exceptions_in_calendar_client')
    else:
        cal=Calendar();cal.add('PRODID','-//Telegram Manager//V24//RU');cal.add('VERSION','2.0')
        component=IEvent();component.add('UID',uid);cal.add_component(component)
    def replace(key,value):
        component.pop(key,None)
        if value is not None: component.add(key,value)
    for field,key in [('title','SUMMARY'),('description','DESCRIPTION'),('location','LOCATION'),('status','STATUS')]:
        if field in changed: replace(key,data[field].upper() if field=='status' else data[field])
    if changed & {'start_at','end_at','all_day','timezone'}:
        zone=ZoneInfo(data['timezone'])
        parse=date.fromisoformat if data['all_day'] else lambda s:datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(zone)
        replace('DTSTART',parse(data['start_at']));replace('DTEND',parse(data['end_at']));component.pop('DURATION',None)
        if not data['all_day'] and data['timezone']!='UTC' and not any(str(z.get('TZID'))==data['timezone'] for z in cal.walk('VTIMEZONE')):
            cal.add_component(Timezone.from_ical(zone_component(data['timezone'])))
    if 'recurrence' in changed:
        replace('RRULE',vRecur.from_ical(data['recurrence']) if data['recurrence'] else None)
    replace('SEQUENCE',int(component.get('SEQUENCE',0))+1)
    stamp=datetime.now(timezone.utc)
    replace('DTSTAMP',stamp);replace('LAST-MODIFIED',stamp)
    result=cal.to_ical().decode('utf-8')
    decode(result,data['timezone'])
    return result
