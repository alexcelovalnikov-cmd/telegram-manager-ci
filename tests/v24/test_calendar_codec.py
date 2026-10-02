"""Requires the real icalendar package; skipped rather than replaced by a stub."""
import pytest
icalendar=pytest.importorskip('icalendar',reason='Real iCalendar dependency is required')
from tm_calendar.codec import encode,decode
from tm_api.v24.common import Rejected

FIELDS={'title':'Созвон с Дмитрием','description':'Проверить материалы\nКириллица','location':'Екатеринбург','start_at':'2026-10-01T15:00:00+05:00','end_at':'2026-10-01T16:00:00+05:00','timezone':'Asia/Yekaterinburg','all_day':False,'recurrence':'FREQ=WEEKLY;COUNT=5','status':'confirmed'}

def test_roundtrip_stable_uid_timezone_and_recurrence():
    text=encode(FIELDS,'stable-uid');item=decode(text)
    assert item['caldav_uid']=='stable-uid'
    assert item['fields']['title']==FIELDS['title']
    assert item['fields']['timezone']==FIELDS['timezone']
    assert item['fields']['recurrence']=='FREQ=WEEKLY;COUNT=5'

def test_all_day_end_is_exclusive_date():
    f=FIELDS|{'all_day':True,'start_at':'2026-10-01','end_at':'2026-10-02','recurrence':None}
    text=encode(f,'date-only');assert 'DTSTART;VALUE=DATE:20261001' in text
    assert decode(text)['fields']['end_at']=='2026-10-02'

def test_unknown_properties_alarms_and_exception_are_preserved():
    cal=icalendar.Calendar.from_ical(encode(FIELDS,'uid'))
    master=cal.walk('VEVENT')[0];master.add('X-USER-DATA','keep')
    alarm=icalendar.Alarm();alarm.add('ACTION','DISPLAY');alarm.add('DESCRIPTION','Напомнить')
    from datetime import timedelta,datetime
    alarm.add('TRIGGER',timedelta(minutes=-15));master.add_component(alarm)
    override=icalendar.Event();override.add('UID','uid');override.add('RECURRENCE-ID',datetime.fromisoformat('2026-10-08T15:00:00+05:00'))
    override.add('DTSTART',datetime.fromisoformat('2026-10-08T16:00:00+05:00'));override.add('DTEND',datetime.fromisoformat('2026-10-08T17:00:00+05:00'));override.add('SUMMARY','Исключение');cal.add_component(override)
    previous=cal.to_ical().decode();updated=encode(FIELDS|{'title':'Новое название'},'uid',previous,{'title'})
    assert 'X-USER-DATA:keep' in updated and 'BEGIN:VALARM' in updated and 'RECURRENCE-ID' in updated
    assert len(icalendar.Calendar.from_ical(updated).walk('VEVENT'))==2
    with pytest.raises(Rejected,match='exceptions'):encode(FIELDS|{'recurrence':'FREQ=DAILY;COUNT=3'},'uid',previous,{'recurrence'})

def test_reject_other_component_types_and_duplicate_uids():
    with pytest.raises(Rejected):decode('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:x\r\nEND:VTODO\r\nEND:VCALENDAR\r\n')
    c=icalendar.Calendar.from_ical(encode(FIELDS,'uid1'))
    c.add_component(icalendar.Calendar.from_ical(encode(FIELDS,'uid2')).walk('VEVENT')[0])
    with pytest.raises(Rejected):decode(c.to_ical().decode())
