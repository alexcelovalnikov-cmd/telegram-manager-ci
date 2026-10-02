"""Durable queue of identifiers/text; originals never enter SQLite."""
import json
import time

MAX_ATTEMPTS = 4
MAX_CRASH_RECOVERIES = 2
PROCESSING_LEASE_SECONDS = 300


class ContentQueue:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS content_jobs (
          chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,source_token TEXT NOT NULL,
          metadata TEXT NOT NULL,retry_stamp TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
          result TEXT,PRIMARY KEY(chat_id,message_id))''')
        db.commit()

    def offer(self, meta, processor_revision=''):
        cid, mid, token = int(meta['chat_id']), int(meta['message_id']), meta['source_token']
        stamp = (meta.get('retry_requested_at') or '') + '|' + str(processor_revision or '')
        current = self.db.execute(
            'SELECT source_token,retry_stamp FROM content_jobs WHERE chat_id=? AND message_id=?',
            (cid,mid)).fetchone()
        if current and current == (token, stamp):
            return False
        self.db.execute(
            'INSERT OR REPLACE INTO content_jobs(chat_id,message_id,source_token,metadata,retry_stamp) VALUES(?,?,?,?,?)',
            (cid,mid,token,json.dumps(meta,ensure_ascii=False),stamp))
        self.db.commit()
        return True
    def peek(self, allowed):
        rows = self.db.execute(
            "SELECT chat_id,message_id,source_token,metadata,retry_stamp,state,attempts,result "
            "FROM content_jobs WHERE state!='done' AND next_attempt<=? "
            "ORDER BY next_attempt,rowid LIMIT 100",
            (time.time(),)).fetchall()
        for row in rows:
            if row[0] in allowed:
                keys=('chat_id','message_id','source_token','metadata',
                      'retry_stamp','state','attempts','result')
                value=dict(zip(keys,row))
                value['metadata']=json.loads(value['metadata'])
                if value['result']:
                    value['result']=json.loads(value['result'])
                return value
        return None

    def same(self, job):
        row=self.db.execute(
            'SELECT source_token,retry_stamp FROM content_jobs WHERE chat_id=? AND message_id=?',
            (job['chat_id'],job['message_id'])).fetchone()
        return row == (job['source_token'],job['retry_stamp'])

    def begin(self, job):
        """Persist an attempt before heavy work so an OOM cannot cause an infinite retry loop."""
        row=self.db.execute(
            'SELECT state,attempts,source_token,retry_stamp FROM content_jobs '
            'WHERE chat_id=? AND message_id=?',
            (job['chat_id'],job['message_id'])).fetchone()
        if not row or row[2:] != (job['source_token'],job['retry_stamp']):
            return None
        state,attempts=row[0],int(row[1])
        if state == 'processing' and attempts >= MAX_CRASH_RECOVERIES:
            result={'processing_status':'failed','processing_error':'processing_crash_retry_exhausted',
                    'extracted_text':'','transcript':'','content_summary':'',
                    'processing_details':{'attempts':attempts,'previous_state':'processing'},
                    'text_is_complete':False}
            self.db.execute(
                "UPDATE content_jobs SET state='result',result=?,next_attempt=0 "
                "WHERE chat_id=? AND message_id=? AND source_token=? AND retry_stamp=?",
                (json.dumps(result,ensure_ascii=False),job['chat_id'],job['message_id'],
                 job['source_token'],job['retry_stamp']))
            self.db.commit()
            out=dict(job,state='result',attempts=attempts,result=result)
            return out
        attempts += 1
        self.db.execute(
            "UPDATE content_jobs SET state='processing',attempts=?,next_attempt=? "
            "WHERE chat_id=? AND message_id=? AND source_token=? AND retry_stamp=?",
            (attempts,time.time()+PROCESSING_LEASE_SECONDS,job['chat_id'],job['message_id'],
             job['source_token'],job['retry_stamp']))
        self.db.commit()
        return dict(job,state='processing',attempts=attempts)

    def result(self, job, result):
        self.db.execute(
            "UPDATE content_jobs SET state='result',result=?,next_attempt=0 "
            "WHERE chat_id=? AND message_id=? AND source_token=? AND retry_stamp=?",
            (json.dumps(result,ensure_ascii=False),job['chat_id'],job['message_id'],
             job['source_token'],job['retry_stamp']))
        self.db.commit()

    def finish(self, job, retry=False):
        attempts=max(1,int(job.get('attempts') or 0))
        state='queued' if retry and attempts<MAX_ATTEMPTS else 'done'
        delay=min(3600,300*2**attempts) if state=='queued' else 0
        self.db.execute(
            'UPDATE content_jobs SET state=?,result=NULL,attempts=?,next_attempt=? '
            'WHERE chat_id=? AND message_id=? AND source_token=? AND retry_stamp=?',
            (state,attempts,time.time()+delay,job['chat_id'],job['message_id'],
             job['source_token'],job['retry_stamp']))
        self.db.commit()

    def defer(self, job, seconds=30):
        self.db.execute(
            "UPDATE content_jobs SET state=CASE WHEN state='processing' THEN 'queued' ELSE state END,"
            "next_attempt=? WHERE chat_id=? AND message_id=? AND source_token=? AND retry_stamp=?",
            (time.time()+seconds,job['chat_id'],job['message_id'],
             job['source_token'],job['retry_stamp']))
        self.db.commit()

    def count(self):
        return self.db.execute(
            "SELECT count(*) FROM content_jobs WHERE state!='done'").fetchone()[0]
