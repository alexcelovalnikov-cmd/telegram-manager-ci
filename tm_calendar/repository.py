"""Canonical calendar writes. API and CalDAV share transactions and the same lock."""
import re
from uuid import UUID, uuid5, NAMESPACE_URL, uuid4
from datetime import datetime,timezone
from tm_api.v24.common import Rejected, check_revision, encoded
from . import codec

CALENDAR_LOCK=74240928

def lock(tx, write=True):
    tx.execute('SELECT pg_advisory_xact_lock(%s)' if write else 'SELECT pg_advisory_xact_lock_shared(%s)',(CALENDAR_LOCK,))

def username(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',value): raise Rejected('invalid_calendar_username')
    return value

def path_segment(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,200}',value) or value in ('.','..'): raise Rejected('invalid_calendar_path')
    return value

def user(tx, name):
    row=tx.one('SELECT username,instance_id,active,revision FROM tm_calendar.users WHERE username=%s',(name,))
    if not row or not row['active']: raise Rejected('calendar_user_inactive')
    return row

def api_user(tx, instance):
    row=tx.one('SELECT username FROM tm_calendar.users WHERE instance_id=%s AND active',(instance,))
    if not row: raise Rejected('calendar_owner_not_provisioned')
    return row['username']

def access(tx, name, calendar_id, write=False, manage=False):
    user(tx,name)
    row=tx.one("SELECT c.*,m.role,m.revision AS membership_revision FROM tm_calendar.calendars c JOIN tm_calendar.members m ON m.calendar_id=c.id WHERE c.id=%s::uuid AND m.username=%s AND m.status='active' AND c.deleted_at IS NULL",(calendar_id,name))
    if not row or (write and row['role'] not in ('owner','editor')) or (manage and row['role']!='owner'):
        raise Rejected('calendar_access_denied')
    return row

def by_path(tx,name,slug,write=False):
    row=tx.one("SELECT id FROM tm_calendar.calendars WHERE slug=%s AND deleted_at IS NULL",(slug,))
    if not row: raise Rejected('calendar_not_found')
    return access(tx,name,row['id'],write)

def create(tx, name, title, zone='Asia/Yekaterinburg', ident=None, slug=None, props=None, component_type="VEVENT"):
    from zoneinfo import ZoneInfo
    account=user(tx,name)
    if not account.get('instance_id'): raise Rejected('only_owner_can_create_calendars')
    if not isinstance(title,str) or not 1<=len(title)<=200: raise Rejected('invalid_calendar_title')
    try: ZoneInfo(zone)
    except (ValueError,KeyError,TypeError): raise Rejected('unknown_calendar_timezone') from None
    ident=ident or str(uuid4());slug=path_segment(slug or ident)
    if component_type not in ('VEVENT','VTODO'):raise Rejected('invalid_collection_component')
    properties=dict(props or {'tag':'VCALENDAR','D:displayname':title})
    properties['C:supported-calendar-component-set']=component_type
    row=tx.one('INSERT INTO tm_calendar.calendars(id,slug,title,timezone,props,component_type) VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s) RETURNING *',(ident,slug,title,zone,encoded(properties),component_type))
    tx.execute("INSERT INTO tm_calendar.members(calendar_id,username,role,status) VALUES(%s::uuid,%s,'owner','active')",(ident,name))
    calendar_audit(tx,name,'create_calendar',ident,None,{'title':title})
    return row

def calendar_audit(tx,name,operation,target,before,after):
    tx.execute('INSERT INTO tm_calendar.audit(actor,operation,target_id,before_state,after_state) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb)',(name,operation,str(target),encoded(before),encoded(after)))

def event_public(row):
    return {k:v for k,v in row.items() if k not in ('icalendar',)}

def event(tx,name,ident,write=False):
    row=tx.one('SELECT * FROM tm_calendar.events WHERE id=%s::uuid AND deleted_at IS NULL',(ident,))
    if not row: raise Rejected('calendar_event_not_found')
    access(tx,name,row['calendar_id'],write)
    return row

def put(tx,name,calendar_id,href,text,expected_revision=None,ident=None,source='caldav'):
    c=access(tx,name,calendar_id,write=True);path_segment(href)
    if c.get('component_type','VEVENT')=='VTODO':
        from tm_reminders import codec as todo_codec
        decoded=todo_codec.decode(text,c['timezone'])
    else:decoded=codec.decode(text,c['timezone'])
    old=tx.one('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND href=%s FOR UPDATE',(calendar_id,href))
    if expected_revision is not None:
        if not old or old['deleted_at'] is not None: raise Rejected('calendar_event_changed')
        check_revision(old['revision'],expected_revision)
    if old and old['caldav_uid']!=decoded['caldav_uid']: raise Rejected('event_uid_is_immutable')
    if old and old['deleted_at']: raise Rejected('deleted_event_requires_explicit_restore')
    if old and old['etag']==decoded['etag']: return old
    conflict=tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND caldav_uid=%s AND href<>%s',(calendar_id,decoded['caldav_uid'],href))
    if conflict: raise Rejected('duplicate_calendar_uid')
    ident=old['id'] if old else ident or str(uuid4())
    if old:
        row=tx.one("UPDATE tm_calendar.events SET fields=%s::jsonb,icalendar=%s,etag=%s,revision=revision+1,updated_by=%s,source=%s,updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING *",(encoded(decoded['fields']),text,decoded['etag'],name,source,ident))
    else:
        row=tx.one('INSERT INTO tm_calendar.events(id,calendar_id,caldav_uid,href,fields,icalendar,etag,created_by,updated_by,source,component_type) VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s) RETURNING *',(ident,calendar_id,decoded['caldav_uid'],href,encoded(decoded['fields']),text,decoded['etag'],name,name,source,c.get('component_type','VEVENT')))
    if source=='caldav' and c.get('component_type')=='VTODO':
        from tm_reminders.bridge import incoming
        incoming(tx,name,row,old)
    changed(tx,calendar_id,href,False)
    calendar_audit(tx,name,'update_event' if old else 'create_event',ident,event_public(old) if old else None,event_public(row))
    return row

def move_event(tx,name,ident,calendar_id,text,expected_revision,source='telegram_manager'):
    """Move one existing VEVENT between writable calendars without changing its ID/UID/href."""
    old=event(tx,name,ident,write=True)
    check_revision(old['revision'],expected_revision)
    destination=access(tx,name,calendar_id,write=True)
    if destination.get('component_type','VEVENT')!='VEVENT' or old.get('component_type','VEVENT')!='VEVENT':
        raise Rejected('calendar_component_mismatch')
    if str(old['calendar_id'])==str(destination['id']):
        return put(tx,name,destination['id'],old['href'],text,expected_revision=expected_revision,ident=ident,source=source)
    path_segment(old['href'])
    decoded=codec.decode(text,destination['timezone'])
    if old['caldav_uid']!=decoded['caldav_uid']:
        raise Rejected('event_uid_is_immutable')
    conflict=tx.one(
        'SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND id<>%s::uuid AND (href=%s OR caldav_uid=%s) LIMIT 1',
        (destination['id'],ident,old['href'],old['caldav_uid']))
    if conflict:
        raise Rejected('calendar_move_conflict')
    before=event_public(old)
    row=tx.one(
        'UPDATE tm_calendar.events SET calendar_id=%s::uuid,fields=%s::jsonb,icalendar=%s,etag=%s,'
        'revision=revision+1,updated_by=%s,source=%s,updated_at=clock_timestamp() '
        'WHERE id=%s::uuid RETURNING *',
        (destination['id'],encoded(decoded['fields']),text,decoded['etag'],name,source,ident))
    changed(tx,old['calendar_id'],old['href'],True)
    changed(tx,destination['id'],old['href'],False)
    calendar_audit(tx,name,'move_event',ident,before,event_public(row))
    return row

def changed(tx,calendar_id,href,deleted):
    c=tx.one('UPDATE tm_calendar.calendars SET revision=revision+1,updated_at=clock_timestamp() WHERE id=%s::uuid RETURNING revision',(calendar_id,))
    tx.execute('INSERT INTO tm_calendar.changes(calendar_id,revision,href,deleted) VALUES(%s::uuid,%s,%s,%s)',(calendar_id,c['revision'],href,deleted))

def delete(tx,name,ident,expected_revision,*,bridge=True):
    row=event(tx,name,ident,write=True);check_revision(row['revision'],expected_revision)
    if row.get('component_type')=='VTODO' and bridge:
        from tm_reminders.bridge import incoming_delete
        incoming_delete(tx,name,row)
    tx.execute('UPDATE tm_calendar.events SET deleted_at=clock_timestamp(),updated_at=clock_timestamp(),revision=revision+1,updated_by=%s WHERE id=%s::uuid',(name,ident))
    changed(tx,row['calendar_id'],row['href'],True)
    calendar_audit(tx,name,'delete_event',ident,event_public(row),{'deleted':True})
    return {'id':ident,'deleted':True,'revision':row['revision']+1}

def member(tx,name,calendar_id,target,role,status,expected_revision):
    access(tx,name,calendar_id,manage=True);username(target)
    if role not in ('viewer','owner','editor') or status not in ('active','suspended','revoked'): raise Rejected('invalid_membership')
    if name==target: raise Rejected('cannot_change_own_owner_access')
    if not tx.one('SELECT username FROM tm_calendar.users WHERE username=%s',(target,)): raise Rejected('create_separate_account_first')
    old=tx.one('SELECT * FROM tm_calendar.members WHERE calendar_id=%s::uuid AND username=%s FOR UPDATE',(calendar_id,target))
    check_revision(old['revision'] if old else 0,expected_revision)
    if old and old['role']=='owner' and (role!='owner' or status!='active'):
        count=tx.one("SELECT count(*) AS n FROM tm_calendar.members WHERE calendar_id=%s::uuid AND role='owner' AND status='active'",(calendar_id,))['n']
        if count<=1: raise Rejected('last_owner_cannot_be_removed')
    row=tx.one("INSERT INTO tm_calendar.members(calendar_id,username,role,status,revoked_at) VALUES(%s::uuid,%s,%s,%s,CASE WHEN %s='revoked' THEN clock_timestamp() END) ON CONFLICT(calendar_id,username) DO UPDATE SET role=EXCLUDED.role,status=EXCLUDED.status,revoked_at=EXCLUDED.revoked_at,revision=tm_calendar.members.revision+1,updated_at=clock_timestamp() RETURNING *",(calendar_id,target,role,status,status))
    calendar_audit(tx,name,'set_member',calendar_id,old,row);return row
