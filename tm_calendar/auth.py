"""Radicale authentication: separately revocable Argon2id app passwords."""
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from radicale.auth import BaseAuth
from .context import database, credential
from .repository import username
from tm_api.v24.common import Rejected

class Auth(BaseAuth):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.hasher = PasswordHasher()
        self.dummy_hash = self.hasher.hash('not-a-real-account-password')

    def _login(self, login, password):
        credential.set(None)
        try:
            username(login)
            if not isinstance(password,str) or len(password)>1024:
                return ''
            with database().transaction(read_only=True) as tx:
                row=tx.one("SELECT u.username,u.password_hash,u.active,u.revision,(u.instance_id IS NOT NULL OR EXISTS(SELECT 1 FROM tm_calendar.members m JOIN tm_calendar.calendars c ON c.id=m.calendar_id WHERE m.username=u.username AND m.status='active' AND c.deleted_at IS NULL)) AS has_access FROM tm_calendar.users u WHERE u.username=%s",(login,))
            self.hasher.verify(row['password_hash'] if row else self.dummy_hash,password)
            if not row or not row['active'] or not row['has_access']:
                return ''
            credential.set((login,row['revision']))
            return login
        except (Rejected,VerificationError,InvalidHashError):
            return ''
        except Exception:
            # Do not disclose DSNs, hashes or underlying database errors.
            return ''
