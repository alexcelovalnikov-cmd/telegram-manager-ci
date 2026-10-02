"""Async facade. Blocking PostgreSQL is run off the ASGI event loop."""
import asyncio
from .database import Database
from .engine import Engine
from .reading import Reader
from .review import Reviews
from .common import Rejected

class DynamicService:
    def __init__(self,config):
        self.database=Database(config.v24_dsn);self.instance=config.instance_id
        self.engine=Engine(self.database,self.instance,getattr(config,"configurable_enabled",False))
        if self.engine.configurable:
            from ..v25.reading import ConfigReader
            self.configuration=ConfigReader(self.database,self.instance)
        self.reader=Reader(self.database,self.instance,config.token_sha256)
        from ..whatsapp import WhatsAppReader
        self.whatsapp=WhatsAppReader(config.whatsapp_db_path,config.token_sha256)
        from ..whatsapp_backfill import WhatsAppBackfillBusiness
        self.engine.whatsapp_backfill=WhatsAppBackfillBusiness(self.instance,self.whatsapp)
        from ..postproduction_sheet_write import PostproductionSheetBusiness
        self.engine.postproduction_sheet=PostproductionSheetBusiness(
            self.instance,config.google_sheets_credentials_path)
        from ..postproduction_reminder_projection import PostproductionReminderBusiness
        self.engine.postproduction_reminders=PostproductionReminderBusiness(
            self.instance,config.google_sheets_credentials_path)
        from ..postproduction_pov_accounting import PovAccountingBusiness
        self.engine.pov_accounting=PovAccountingBusiness(
            self.instance,config.google_sheets_credentials_path)
        from ..frameio import FrameIOOAuth,FrameIOReader
        frameio_oauth=FrameIOOAuth(
            enabled=config.frameio_enabled,
            client_id=config.frameio_client_id,
            client_secret_path=config.frameio_client_secret_path,
            state_path=config.frameio_state_path,
            redirect_uri=config.frameio_redirect_uri or config.public_url.rstrip("/")+"/frameio/oauth/callback",
            scopes=config.frameio_scopes,
        )
        self.frameio=FrameIOReader(frameio_oauth)
        self.reviews=Reviews(self.engine)
        from ..v32.reconciliation import Reconciliation
        self.reconciliation=Reconciliation(self.database,self.instance,self.reader,self.reviews,self.engine.configurable)
        self._semaphore=asyncio.Semaphore(6)
    async def run(self,function,*args,**kwargs):
        async with self._semaphore:
            return await asyncio.to_thread(function,*args,**kwargs)
    def calendar_read(self,operation,calendar_id=None,event_id=None,after_id=None,limit=50):
        from tm_calendar import repository as repo
        with self.database.transaction(read_only=True) as tx:
            repo.lock(tx,write=False);name=repo.api_user(tx,self.instance)
            if operation=='list_calendars':
                rows=tx.all("SELECT c.id,c.slug,c.title,c.timezone,c.revision,m.role FROM tm_calendar.calendars c JOIN tm_calendar.members m ON m.calendar_id=c.id WHERE m.username=%s AND m.status='active' AND c.deleted_at IS NULL AND c.component_type='VEVENT' ORDER BY c.id",(name,))
                return {'items':rows,'username':name,'source':'server_postgresql'}
            if operation=='get_calendar_event':
                row=repo.event(tx,name,event_id)
                if row.get('component_type','VEVENT')!='VEVENT':raise Rejected('use_get_server_reminder')
                return {'event':repo.event_public(row)}
            if calendar_id is None:raise Rejected('calendar_id_required')
            c=repo.access(tx,name,calendar_id,manage=operation=='list_calendar_members')
            if c.get('component_type','VEVENT')!='VEVENT' and operation!='list_calendar_members':raise Rejected('use_list_server_reminders')
            if operation=='list_calendar_members':
                return {'items':tx.all('SELECT m.username,m.role,m.status,m.revision,m.revoked_at,u.active AS account_active FROM tm_calendar.members m JOIN tm_calendar.users u ON u.username=m.username WHERE m.calendar_id=%s::uuid ORDER BY m.username',(calendar_id,))}
            if operation!='list_calendar_events':raise Rejected('unknown_calendar_read')
            if not 1<=limit<=100:raise Rejected('invalid_limit')
            rows=tx.all('SELECT * FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s',(calendar_id,after_id,after_id,limit+1))
            return {'items':[repo.event_public(r) for r in rows[:limit]],'next_after_id':rows[limit-1]['id'] if len(rows)>limit else None,
                    'recurrences':'stored_series_with_exceptions_not_expanded','calendar_revision':c['revision']}
