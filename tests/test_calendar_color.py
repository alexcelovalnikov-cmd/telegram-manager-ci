import pytest

from tm_api.v24.common import Rejected
from tm_calendar.storage import (
    Collection,
    calendar_color,
    is_local_calendar_color,
    metadata,
    without_local_calendar_color,
)


def test_apple_event_calendar_color_is_local_only():
    props={
        'tag':'VCALENDAR',
        'D:displayname':'Общий календарь',
        'ICAL:calendar-color':'#0088FF',
        'C:supported-calendar-component-set':'VEVENT',
    }
    filtered=without_local_calendar_color(props)
    assert 'ICAL:calendar-color' not in filtered
    assert filtered['D:displayname']=='Общий календарь'
    assert is_local_calendar_color('ICAL:calendar-color')
    assert is_local_calendar_color('{http://apple.com/ns/ical/}calendar-color')


def test_event_metadata_does_not_persist_calendar_color():
    result=metadata({
        'tag':'VCALENDAR',
        'D:displayname':'Общий календарь',
        'ICAL:calendar-color':'#FF0000',
        'C:supported-calendar-component-set':'VEVENT',
    })
    assert 'ICAL:calendar-color' not in result
    assert result['C:supported-calendar-component-set']=='VEVENT'


def test_reminder_list_metadata_persists_and_normalizes_color():
    result=metadata({
        'tag':'VCALENDAR',
        'D:displayname':'Проекты',
        '{http://apple.com/ns/ical/}calendar-color':'#ff383c',
        'C:supported-calendar-component-set':'VTODO',
    })
    assert result['ICAL:calendar-color']=='#FF383C'
    assert result['C:supported-calendar-component-set']=='VTODO'
    assert calendar_color(result)=='#FF383C'


def test_reminder_list_collection_advertises_saved_color():
    row={
        'id':'00000000-0000-4000-8000-000000000001',
        'revision':3,
        'updated_at':'2026-09-30T10:00:00+00:00',
        'component_type':'VTODO',
        'props':{
            'tag':'VCALENDAR',
            'D:displayname':'Рентал',
            'C:supported-calendar-component-set':'VTODO',
            'ICAL:calendar-color':'#83D754',
        },
    }
    collection=Collection('owner/rental',row['id'],snapshot=row,username='owner')
    assert collection.get_meta('ICAL:calendar-color')=='#83D754'
    assert collection.get_meta('{http://apple.com/ns/ical/}calendar-color')=='#83D754'
    assert collection.get_meta()['ICAL:calendar-color']=='#83D754'


def test_event_collection_still_hides_saved_color():
    row={
        'id':'00000000-0000-4000-8000-000000000002',
        'revision':3,
        'updated_at':'2026-09-30T10:00:00+00:00',
        'component_type':'VEVENT',
        'props':{
            'tag':'VCALENDAR',
            'D:displayname':'Планы',
            'C:supported-calendar-component-set':'VEVENT',
            'ICAL:calendar-color':'#FF0000',
        },
    }
    collection=Collection('owner/plans',row['id'],snapshot=row,username='owner')
    assert collection.get_meta('ICAL:calendar-color') is None
    assert 'ICAL:calendar-color' not in collection.get_meta()


@pytest.mark.parametrize('value',['red','#FFF','#GG0000','#12345','#123456789'])
def test_invalid_reminder_list_color_is_rejected(value):
    with pytest.raises(Rejected,match='invalid_calendar_color'):
        metadata({
            'tag':'VCALENDAR',
            'D:displayname':'Проекты',
            'ICAL:calendar-color':value,
            'C:supported-calendar-component-set':'VTODO',
        })
