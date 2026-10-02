"""Two durable, bounded server jobs. Neither can mutate business or native state."""
import asyncio
import os
from pathlib import Path
import sqlite3
import time

JOBS = ('health_monitor', 'review_preparation', 'rcc_sheet_sync')
TABLE = 'jobs_v75'

class JobStore:
    def __init__(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if p.is_symlink():
            raise ValueError('Unsafe queue path')
        self.worker_error = False
        self.db = sqlite3.connect(p)
        os.chmod(p, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs_v75 (name TEXT PRIMARY KEY CHECK(name IN ('health_monitor','review_preparation','rcc_sheet_sync')), due REAL NOT NULL, running INTEGER NOT NULL DEFAULT 0, outcome TEXT, last_success REAL, attempts INTEGER NOT NULL DEFAULT 0)")
        self.db.executemany('INSERT OR IGNORE INTO jobs_v75(name,due) VALUES (?,0)', [(name,) for name in JOBS])
        # A process restart retries interrupted bounded jobs. Business state is
        # canonical in PostgreSQL; the external sheet projection is idempotent.
        self.db.execute('UPDATE jobs_v75 SET due=0,running=0 WHERE running=1')
        self.db.commit()

    def claim(self, now):
        with self.db:
            row = self.db.execute('SELECT name,outcome FROM jobs_v75 WHERE due<=? ORDER BY due,name LIMIT 1', (now,)).fetchone()
            if row:
                self.db.execute('UPDATE jobs_v75 SET running=1,due=?,attempts=attempts+1 WHERE name=?', (now+60,row[0]))
            return row

    def finish(self, name, outcome, now):
        delay = 300 if name == 'review_preparation' else 60
        with self.db:
            self.db.execute('UPDATE jobs_v75 SET running=0,outcome=?,due=?,last_success=CASE WHEN ? IN (\'ok\',\'observe\',\'disabled\') THEN ? ELSE last_success END WHERE name=?',
                            (outcome, now + (delay if outcome != 'unavailable' else 30), outcome, now, name))

    def status(self):
        rows=self.db.execute('SELECT name,running,outcome,last_success,due FROM jobs_v75 ORDER BY name').fetchall()
        return {'enabled':True,'worker_healthy':not self.worker_error,'capacity':len(JOBS),'durable':True,'jobs':[
            {'name':n,'running':bool(r),'outcome':o,'last_success_unix':s,'next_run_unix':d} for n,r,o,s,d in rows]}

    def close(self):
        self.db.close()

class Worker:
    def __init__(self, store, service, audit):
        self.store, self.service, self.audit = store, service, audit

    async def once(self, now=None):
        now = time.time() if now is None else now
        claimed = self.store.claim(now)
        if not claimed:
            return False
        name, before = claimed
        try:
            if name == 'health_monitor':
                result=await self.service.get_system_status()
                outcome='ok' if result.get('production_runtime_available', result.get('mac_available', False)) else 'runtime_offline'
            elif name == 'review_preparation':
                # Existing stable V18 SQL prepares current context, without marking
                # a question shown, selecting focus or applying an action.
                await self.service.get_review_bundle(explicit=False,limit=20)
                outcome='ok'
            elif name == 'rcc_sheet_sync':
                result=await self.service.sync_rcc_sheet()
                status=result.get('status')
                outcome=status if status in ('ok','observe','disabled') else 'unavailable'
            else:
                raise RuntimeError('Unknown job')
        except Exception:
            outcome='unavailable'
        # Keep only transition metadata in the append-only audit, no source content.
        if outcome != before:
            self.audit.record('job_'+name, 'recovered' if outcome=='ok' and before else outcome)
        self.store.finish(name,outcome,now)
        return True

    async def run(self):
        while True:
            try:
                await self.once()
                self.store.worker_error = False
            except Exception:
                self.store.worker_error = True
            await asyncio.sleep(2)
