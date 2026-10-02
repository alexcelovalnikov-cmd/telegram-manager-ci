"""Requires the actual pinned iCalendar library, never a replacement stub."""
import pytest
pytest.importorskip('icalendar')
from icalendar import Calendar,Alarm
from tm_reminders.codec import encode,decode
from tm_api.v24.common import Rejected

BASE={'title':'#созвон Созвон с Дмитрием','description':'Кириллица; запятая, перенос\nещё строка'}

def test_roundtrip_no_due():
    text=encode(BASE,'stable@tm')
    d=decode(text)
    assert d['caldav_uid']=='stable@tm' and d['fields']['description']==BASE['description']
    assert d['fields']['due_at'] is None
    assert d['fields']['categories']==[]
    assert 'CATEGORIES:' not in text

def test_legacy_empty_categories_are_normalized():
    text=encode(BASE|{'categories':['tag']},'stable@tm').replace('CATEGORIES:tag','CATEGORIES:')
    assert decode(text)['fields']['categories']==[]

def test_all_day_and_completed():
    text=encode(BASE|{'due_at':'2026-10-01','all_day':True,'status':'completed'},'stable@tm')
    assert 'DUE;VALUE=DATE:20261001' in text and 'STATUS:COMPLETED' in text
    assert decode(text)['fields']['all_day']
    assert 'COMPLETED:' in text

def test_timed_timezone_and_unicode_long_description():
    data=BASE|{'due_at':'2026-10-01T15:00:00+05:00','timezone':'Asia/Yekaterinburg','description':'Абв; ,\n'*1000}
    assert decode(encode(data,'stable@tm'))['fields']['description']==data['description']

def test_metadata_and_alarm_survive_edit():
    first=Calendar.from_ical(encode(BASE,'stable@tm'));todo=first.walk('VTODO')[0]
    todo.add('X-PRIVATE-TEST','preserve')
    a=Alarm();a.add('ACTION','DISPLAY');a.add('DESCRIPTION','Напомнить')
    from datetime import timedelta
    a.add('TRIGGER',timedelta(minutes=-10));todo.add_component(a)
    previous=first.to_ical().decode();fields=decode(previous)['fields']|{'title':'Изменено'}
    result=Calendar.from_ical(encode(fields,'stable@tm',previous,changed={'title'}))
    assert str(result.walk('VTODO')[0]['X-PRIVATE-TEST'])=='preserve'
    assert len(result.walk('VALARM'))==1

def test_event_cannot_be_written_to_task_list():
    event='BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261001T000000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
    with pytest.raises(Rejected):decode(event)

def test_reopen_removes_completed_stamp():
    first=encode(BASE|{'status':'completed'},'stable@tm')
    fields=decode(first)['fields']|{'status':'open'}
    t=Calendar.from_ical(encode(fields,'stable@tm',first,changed={'status'})).walk('VTODO')[0]
    assert 'COMPLETED' not in t and str(t['STATUS'])=='NEEDS-ACTION'
