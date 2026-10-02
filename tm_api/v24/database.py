"""Private PostgreSQL transport. No caller-controlled SQL or identifiers."""
from contextlib import contextmanager
from .common import clean

class Transaction:
    def __init__(self, connection):
        self.connection=connection
    def execute(self, statement, params=()):
        return self.connection.execute(statement, params)
    def one(self, statement, params=()):
        row=self.execute(statement,params).fetchone()
        return clean(row) if row is not None else None
    def all(self, statement, params=()):
        return clean(self.execute(statement,params).fetchall())

class Database:
    def __init__(self, dsn):
        if not dsn:
            raise ValueError('TM_V24_DSN is required')
        self._dsn=dsn
    @contextmanager
    def transaction(self, read_only=False):
        import psycopg
        from psycopg.rows import dict_row
        # Never log connection exceptions/DSNs or raw SQL containing user data.
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5,
                options='-c search_path=pg_catalog -c statement_timeout=15000 -c lock_timeout=5000') as c:
            if read_only:
                c.execute('SET TRANSACTION READ ONLY')
            yield Transaction(c)
