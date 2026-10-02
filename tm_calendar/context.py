"""One authenticated request, one canonical PostgreSQL transaction."""
from contextvars import ContextVar
import os
from tm_api.v24.database import Database
from tm_api.v24.common import Rejected

current = ContextVar('tm_calendar_transaction', default=None)
credential = ContextVar('tm_calendar_credential', default=None)

def database():
    return Database(os.environ.get('TM_CALENDAR_DSN', ''))

def transaction():
    value = current.get()
    if value is None:
        raise Rejected('calendar_transaction_required')
    return value
