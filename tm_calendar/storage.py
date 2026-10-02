"""PostgreSQL-backed Radicale storage; no filesystem mirror or async write gap.

HTTP/CalDAV parsing, REPORT, recurrence filtering and ETag preconditions belong
in Radicale. This adapter owns only persistence, ACLs, locks and sync tokens.
"""
from contextlib import contextmanager
from functools import wraps
from datetime import datetime,timezone
from email.utils import format_datetime
from uuid import uuid4
from radicale.storage import BaseStorage,BaseCollection,ComponentNotFoundError,ComponentExistsError
from radicale.item import Item
from .context import database,current,credential,transaction
from . import repository as repo
from tm_api.v24.common import Rejected,encoded


def http_date(value):
    return format_datetime(datetime.fromisoformat(value).astimezone(timezone.utc),usegmt=True)

def parts(path):
    values=path.strip('/').split('/') if path.strip('/') else []
    for value in values: repo.path_segment(value)
    if len(values)>3: raise Rejected('invalid_calendar_path')
    return values

def is_local_calendar_color(key):
    value=str(key).lower()
    return value=='ical:calendar-color' or value=='{http://apple.com/ns/ical/}calendar-color'

def without_local_calendar_color(props):
    return {key:value for key,value in dict(props or {}).items() if not is_local_calendar_color(key)}

def calendar_color(props):
    for key,value in dict(props or {}).items():
        if is_local_calendar_color(key):
            return value
    return None

def normalized_calendar_color(value):
    import re
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?',value):
        raise Rejected('invalid_calendar_color')
    return value.upper()

def metadata(props,component_type=None):
    props=dict(props or {})
    declared=props.get('C:supported-calendar-component-set')
    component=component_type or declared or 'VEVENT'
    if component not in ('VEVENT','VTODO'):raise Rejected('one_collection_component_required')
    if declared is not None and declared!=component:raise Rejected('collection_component_immutable')
    color=calendar_color(props)
    props=without_local_calendar_color(props)
    if component=='VTODO' and color is not None:
        props['ICAL:calendar-color']=normalized_calendar_color(color)
    if props.get('tag','VCALENDAR')!='VCALENDAR':
        raise Rejected('only_event_calendars_supported')
    for key,value in props.items():
        if not isinstance(key,str) or not isinstance(value,str) or len(key)>200 or len(value)>30000:
            raise Rejected('invalid_calendar_properties')
    if len(encoded(props).encode())>60000: raise Rejected('calendar_properties_too_large')
    props['C:supported-calendar-component-set']=component
    props['tag']='VCALENDAR'
    return props

def atomic_write(function):
    """Rollback before Radicale catches a storage exception and returns an HTTP error."""
    @wraps(function)
    def wrapped(*args,**kwargs):
        tx,_,mode=transaction()
        if mode!='w':raise Rejected('write_transaction_required')
        try:
            with tx.connection.transaction():
                return function(*args,**kwargs)
        except Rejected:
            raise
        except (ComponentNotFoundError,ComponentExistsError):
            raise
        except Exception:
            raise Rejected('calendar_write_failed') from None
    return wrapped

class Collection(BaseCollection):
    def __init__(self,path,calendar_id=None,snapshot=None,item_snapshot=None,username=None):
        self._path=path.strip('/');self.calendar_id=calendar_id;self._username=username
        self._snapshot=dict(snapshot) if snapshot is not None else None
        self._item_snapshot=tuple(dict(row) for row in item_snapshot) if item_snapshot is not None else None
    @property
    def path(self):return self._path
    def _state(self,write=False,manage=False,metadata=False):
        # Radicale renders collection metadata after Storage.acquire_lock() has
        # already exited while serializing PROPFIND responses. A collection created
        # by discover() therefore keeps read-only row snapshots for metadata and,
        # when needed by collection serialization, event rows from the same transaction.
        # Writes still require a live PostgreSQL transaction.
        value=current.get()
        if self.calendar_id is None:
            if value is None:
                if write or manage:raise Rejected('calendar_transaction_required')
                return None,None,None
            tx,name,mode=value
            if write and mode!='w':raise Rejected('write_transaction_required')
            return tx,name,None
        if value is None:
            if metadata and self._snapshot is not None and not write and not manage:
                return None,None,self._snapshot
            raise Rejected('calendar_transaction_required')
        tx,name,mode=value
        if write and mode!='w':raise Rejected('write_transaction_required')
        row=repo.access(tx,name,self.calendar_id,write=write,manage=manage)
        if not write and not manage:self._snapshot=dict(row)
        return tx,name,row
    def _item(self,row):
        return Item(collection=self,href=row['href'],text=row['icalendar'],etag=row['etag'],uid=row['caldav_uid'],last_modified=http_date(row['updated_at']))
    @property
    def etag(self):
        _,_,row=self._state(metadata=True)
        return '"tm-'+row['id']+'-'+str(row['revision'])+'"' if row else '"tm-root"'
    @property
    def last_modified(self):
        _,_,row=self._state(metadata=True)
        return http_date(row['updated_at']) if row else 'Mon, 01 Jan 2024 00:00:00 GMT'
    def get_meta(self,key=None):
        _,_,row=self._state(metadata=True)
        raw=row['props'] if row else {}
        component=row.get('component_type','VEVENT') if row else 'VEVENT'
        if component=='VTODO':
            props=metadata(raw,component_type='VTODO')
            if key is not None and is_local_calendar_color(key):return calendar_color(props)
        else:
            props=without_local_calendar_color(raw)
            if key is not None and is_local_calendar_color(key):return None
        return props.get(key) if key is not None else dict(props)
    @atomic_write
    def set_meta(self,props):
        tx,name,row=self._state(write=True,manage=True)
        if not row:raise Rejected('principal_properties_not_writable')
        props=metadata(props,component_type=row.get('component_type','VEVENT'))
        title=props.get('D:displayname',row['title'])
        if not title or len(title)>200:raise Rejected('invalid_calendar_title')
        tx.execute('UPDATE tm_calendar.calendars SET props=%s::jsonb,title=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s::uuid',(encoded(props),title,self.calendar_id))
        repo.calendar_audit(tx,name,'set_calendar_properties',self.calendar_id,row['props'],props)
    def get_multi(self,hrefs):
        wanted=list(dict.fromkeys(hrefs))
        for href in wanted: repo.path_segment(href)
        if current.get() is None:
            if self._item_snapshot is None:
                if self.calendar_id is None:return
                raise Rejected('calendar_transaction_required')
            by_href={row['href']:row for row in self._item_snapshot}
            for href in wanted:
                value=by_href.get(href)
                yield href,self._item(value) if value else None
            return
        tx,_,row=self._state()
        for href in wanted:
            value=tx.one('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND href=%s AND deleted_at IS NULL',(self.calendar_id,href)) if row else None
            yield href,self._item(value) if value else None
    def get_all(self):
        # Radicale may serialize a collection after Storage.acquire_lock() has
        # exited while building PROPFIND properties such as getcontentlength.
        # In that case use the immutable rows captured by discover() inside the
        # read transaction instead of touching PostgreSQL without a lock.
        if current.get() is None:
            if self._item_snapshot is not None:
                for value in self._item_snapshot:yield self._item(value)
                return
            if self.calendar_id is None:return
            raise Rejected('calendar_transaction_required')
        tx,_,row=self._state()
        if not row:return
        # Keyset pages keep SQL/result memory bounded. The request holds its lock.
        cursor=None
        while True:
            rows=tx.all('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT 100',(self.calendar_id,cursor,cursor))
            if not rows:break
            for value in rows:yield self._item(value)
            cursor=rows[-1]['id']
    def has_uid(self,uid):
        tx,_,row=self._state()
        return bool(row and tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND caldav_uid=%s AND deleted_at IS NULL',(self.calendar_id,uid)))
    @atomic_write
    def upload(self,href,item):
        tx,name,_=self._state(write=True);repo.path_segment(href)
        old=next(self.get_multi([href]))[1]
        row=repo.put(tx,name,self.calendar_id,href,item.serialize())
        return self._item(row),old
    @atomic_write
    def delete(self,href=None):
        tx,name,row=self._state(write=True,manage=href is None)
        if not row:raise Rejected('principal_cannot_be_deleted')
        if href is not None:
            repo.path_segment(href)
            event=tx.one('SELECT id,revision FROM tm_calendar.events WHERE calendar_id=%s::uuid AND href=%s AND deleted_at IS NULL',(self.calendar_id,href))
            if not event:raise ComponentNotFoundError(href)
            repo.delete(tx,name,event['id'],event['revision']);return
        if row.get('component_type')=='VTODO' and tx.one("SELECT id FROM tm_config.workspaces WHERE settings->'reminder_list'->>'id'=%s AND status='active'",(self.calendar_id,)):
            raise Rejected('workspace_reminder_list_requires_explicit_configuration_change')
        for event in tx.all('SELECT id,revision FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL',(self.calendar_id,)):
            repo.delete(tx,name,event['id'],event['revision'])
        tx.execute('UPDATE tm_calendar.calendars SET deleted_at=clock_timestamp(),updated_at=clock_timestamp(),revision=revision+1 WHERE id=%s::uuid',(self.calendar_id,))
        repo.calendar_audit(tx,name,'delete_calendar',self.calendar_id,{'title':row['title']},{'deleted':True})
    def _sync_in_tx(self,tx,row,old_token=''):
        if not row:return 'http://telegram-manager.invalid/sync/root',[]
        prefix='http://telegram-manager.invalid/sync/'+self.calendar_id+'/'
        token=prefix+str(row['revision'])
        if not old_token:
            return token,[r['href'] for r in tx.all('SELECT href FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL ORDER BY href',(self.calendar_id,))]
        if not old_token.startswith(prefix):raise ValueError('Invalid sync token')
        value=old_token[len(prefix):]
        if not value.isdigit() or not 1<=int(value)<=row['revision']:raise ValueError('Invalid sync token')
        return token,[r['href'] for r in tx.all('SELECT DISTINCT href FROM tm_calendar.changes WHERE calendar_id=%s::uuid AND revision>%s ORDER BY href',(self.calendar_id,int(value)))]

    def sync(self,old_token=''):
        # Radicale can serialize sync-token data after acquire_lock() has exited.
        # For initial discovery the immutable item snapshot is enough. For an
        # incremental sync token we need the canonical change log, so reopen a
        # bounded read-only transaction and re-check the authenticated user's ACL.
        if current.get() is None:
            row=self._snapshot
            if not row:
                if self.calendar_id is None:
                    return 'http://telegram-manager.invalid/sync/root',[]
                raise Rejected('calendar_transaction_required')
            prefix='http://telegram-manager.invalid/sync/'+self.calendar_id+'/'
            token=prefix+str(row['revision'])
            if not old_token:
                if self._item_snapshot is None:
                    raise Rejected('calendar_transaction_required')
                return token,sorted(r['href'] for r in self._item_snapshot)
            if not self._username:
                raise Rejected('calendar_user_required')
            with database().transaction(read_only=True) as tx:
                fresh=repo.access(tx,self._username,self.calendar_id)
                self._snapshot=dict(fresh)
                return self._sync_in_tx(tx,fresh,old_token)

        tx,_,row=self._state()
        return self._sync_in_tx(tx,row,old_token)

class Storage(BaseStorage):
    _is_collision_free=True
    @contextmanager
    def acquire_lock(self,mode,user='',*args,**kwargs):
        if mode not in ('r','w'):raise Rejected('invalid_storage_lock')
        with database().transaction(read_only=mode=='r') as tx:
            repo.lock(tx,write=mode=='w')
            if user:
                account=repo.user(tx,user);auth=credential.get()
                if auth is None or auth!=(user,account['revision']):raise Rejected('credential_changed')
            marker=current.set((tx,user,mode))
            try:yield
            finally:current.reset(marker)
    def discover(self,path,depth='0',child_context_manager=None,user_groups=None):
        values=parts(path);tx,name,_=transaction()
        if not values:
            yield Collection('',username=name)
            if depth!='0' and name:yield Collection(name,username=name)
            return
        if not name or values[0]!=name:return
        repo.user(tx,name)
        if len(values)==1:
            yield Collection(name,username=name)
            if depth!='0':
                rows=tx.all("SELECT c.* FROM tm_calendar.calendars c JOIN tm_calendar.members m ON m.calendar_id=c.id WHERE m.username=%s AND m.status='active' AND c.deleted_at IS NULL ORDER BY c.id",(name,))
                for row in rows:
                    items=tx.all('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL ORDER BY id',(row['id'],))
                    yield Collection(name+'/'+row['slug'],row['id'],row,items,username=name)
            return
        try:row=repo.by_path(tx,name,values[1])
        except Rejected:return
        item_snapshot=None
        if len(values)==2:
            item_snapshot=tx.all('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL ORDER BY id',(row['id'],))
        collection=Collection(name+'/'+values[1],row['id'],row,item_snapshot,username=name)
        if len(values)==2:
            yield collection
            if depth!='0':yield from collection.get_all()
        else:
            item=next(collection.get_multi([values[2]]))[1]
            if item is not None:yield item
    @atomic_write
    def create_collection(self,href,items=None,props=None):
        values=parts(href);tx,name,mode=transaction()
        if mode!='w' or not name or not values or values[0]!=name:raise Rejected('calendar_access_denied')
        repo.user(tx,name)
        if len(values)==1 and not props and items is None:return Collection(name,username=name),{},[]
        if len(values)!=2:raise Rejected('calendar_path_required')
        existing=tx.one('SELECT id FROM tm_calendar.calendars WHERE slug=%s',(values[1],))
        if existing:
            existing_row=repo.access(tx,name,existing['id'],manage=True)
            if props is None and items is None:return Collection('/'.join(values),existing['id'],existing_row,username=name),{},[]
            # Do not silently replace a shared collection from an entire-calendar PUT.
            raise Rejected('whole_calendar_replacement_requires_explicit_migration')
        props=metadata(props);row=repo.create(tx,name,props.get('D:displayname',values[1]),slug=values[1],props=props,component_type=props['C:supported-calendar-component-set'])
        collection=Collection('/'.join(values),row['id'],row,username=name);uploaded={}
        for index,item in enumerate(items or []):
            if index>=1000:raise Rejected('calendar_import_too_large')
            item_href=str(uuid4())+'.ics';new,_=collection.upload(item_href,item);uploaded[item_href]=new
        return collection,uploaded,[]
    @atomic_write
    def move(self,item,to_collection,to_href):
        tx,name,mode=transaction()
        if mode!='w':raise Rejected('write_transaction_required')
        source=item.collection
        if not isinstance(source,Collection) or not isinstance(to_collection,Collection):raise Rejected('invalid_calendar_move')
        src=repo.access(tx,name,source.calendar_id,write=True);dst=repo.access(tx,name,to_collection.calendar_id,write=True)
        if src.get('component_type','VEVENT')!=dst.get('component_type','VEVENT'):raise Rejected('collection_type_mismatch')
        repo.path_segment(to_href)
        old=tx.one('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND href=%s AND deleted_at IS NULL FOR UPDATE',(source.calendar_id,item.href))
        if not old:raise ComponentNotFoundError(item.href)
        if old.get('component_type')=='VTODO' and tx.one('SELECT task_id FROM tm_config.reminder_bindings WHERE resource_id=%s::uuid',(old['id'],)):
            raise Rejected('bound_task_move_requires_tm_preview')
        if source.calendar_id==to_collection.calendar_id and item.href==to_href:return
        conflict=tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND (href=%s OR caldav_uid=%s)',(to_collection.calendar_id,to_href,old['caldav_uid']))
        if conflict and conflict['id']!=old['id']:raise ComponentExistsError(to_href)
        tx.execute('UPDATE tm_calendar.events SET calendar_id=%s::uuid,href=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s::uuid',(to_collection.calendar_id,to_href,name,old['id']))
        repo.changed(tx,source.calendar_id,item.href,True);repo.changed(tx,to_collection.calendar_id,to_href,False)
        repo.calendar_audit(tx,name,'move_event',old['id'],{'calendar_id':source.calendar_id,'href':item.href},{'calendar_id':to_collection.calendar_id,'href':to_href})
    def verify(self):
        with database().transaction(read_only=True) as tx:
            tx.one('SELECT 1 AS ok FROM tm_calendar.calendars LIMIT 1')
        return True
