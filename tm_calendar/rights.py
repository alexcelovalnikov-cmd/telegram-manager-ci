"""ACL checks for discovery and every DAV request, not client-side read-only UI."""
from radicale.rights import BaseRights
from .context import database, current, credential
from . import repository as repo
from tm_api.v24.common import Rejected

def permitted(tx,user,path):
    if not user:
        return ''
    account=repo.user(tx,user)
    auth=credential.get()
    if auth is not None and auth!=(user,account['revision']):
        return ''
    parts=path.strip('/').split('/') if path.strip('/') else []
    if not parts:
        return 'R'
    if parts[0]!=user or len(parts)>3:
        return ''
    if len(parts)==1:
        return 'RW' if account.get('instance_id') else 'R'
    try:
        calendar=repo.by_path(tx,user,parts[1])
    except Rejected:
        # Collection creation is restricted separately by storage.create_collection.
        exists=tx.one('SELECT id FROM tm_calendar.calendars WHERE slug=%s',(parts[1],))
        return 'rw' if len(parts)==2 and not exists and account.get('instance_id') else ''
    if len(parts)==3:
        return ''  # Radicale obtains item permissions from its parent collection.
    return 'rw' if calendar['role'] in ('owner','editor') else 'r'

class Rights(BaseRights):
    def authorization(self,user,path):
        try:
            ctx=current.get()
            if ctx is not None:
                return permitted(ctx[0],user,path)
            with database().transaction(read_only=True) as tx:
                return permitted(tx,user,path)
        except Exception:
            return ''
