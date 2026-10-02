#!/usr/bin/env python3
"""Server-configured Telegram collector. --check never opens a Telegram session.

The one running worker resolves requested dialog metadata and refreshes group
membership. It never joins chats, sends messages, or imports unselected history.
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import time
import tempfile
import sys
import shutil
from types import SimpleNamespace
from tm_media import DEFAULTS, MediaError, TempMedia, LimitedWriter, media_meta, policy, skip_reason, blank_result, normalize_result, run_processor_request
from tm_content_queue import ContentQueue
from tm_source_discovery import (DIALOG_DISCOVERY_PUBLISH_LIMIT, DIALOG_DISCOVERY_SCAN_LIMIT, discovery_due,
    publish_candidates, read_candidates, postproduction_topic_allowed, postproduction_topic_kind,
    discovery_bundle, activation_candidates, excluded_reason)
from pathlib import Path

from rr_common import APP_DIR, database, get_config, get_source_exclusions, load_env, lock_file, now_iso, safe_error, save_health
from rr_groups import match_target

REFRESH_SECONDS = 30
CONFIG_MAX_AGE = 120
DIALOG_DISCOVERY_SECONDS = 600
MEDIA_PROCESSOR_REVISION = 'server-linux-v4-memory-hardening'


def cgroup_memory_stats():
    root=Path('/sys/fs/cgroup')
    def value(name):
        try:
            raw=(root/name).read_text().strip()
            return None if raw=='max' else int(raw)
        except Exception:
            return None
    events={}
    try:
        for line in (root/'memory.events').read_text().splitlines():
            key,raw=line.split(None,1)
            events[key]=int(raw)
    except Exception:
        pass
    return {
        'current_bytes':value('memory.current'),
        'max_bytes':value('memory.max'),
        'peak_bytes':value('memory.peak'),
        'oom_count':events.get('oom'),
        'oom_kill_count':events.get('oom_kill'),
    }


class Outbox:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        os.chmod(str(path), 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(chat_id,message_id))')
        self.db.commit()
        self.content = ContentQueue(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS message_index (message_id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS dynamic_backfill_done (target_id INTEGER NOT NULL,request_revision INTEGER NOT NULL,completed_at TEXT NOT NULL,PRIMARY KEY(target_id,request_revision))')
        self.db.execute('CREATE TABLE IF NOT EXISTS explicit_backfill_done (group_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,completed_at TEXT NOT NULL,PRIMARY KEY(group_id,chat_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS local_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        row=self.db.execute("SELECT value FROM local_meta WHERE key='dynamic_backfill_revision'").fetchone()
        if not row or row[0] != '2':
            self.db.execute('DELETE FROM dynamic_backfill_done')
            self.db.execute("INSERT OR REPLACE INTO local_meta VALUES ('dynamic_backfill_revision','2')")
        self.db.commit()

    def add(self, user, message):
        self.db.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?)',
                        (message['chat_id'], message['message_id'], json.dumps({'user': user, 'message': message}, ensure_ascii=False)))
        self.db.commit()

    def rows(self):
        return self.db.execute('SELECT chat_id,message_id,payload FROM pending ORDER BY rowid LIMIT 100').fetchall()

    def remove(self, chat_id, message_id, expected_payload=None):
        if expected_payload is None:
            self.db.execute('DELETE FROM pending WHERE chat_id=? AND message_id=?', (chat_id,message_id))
        else:
            self.db.execute('DELETE FROM pending WHERE chat_id=? AND message_id=? AND payload=?', (chat_id,message_id,expected_payload))
        self.db.commit()

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0]

    def dynamic_backfill_done(self,target_id,request_revision):
        return bool(self.db.execute(
            'SELECT 1 FROM dynamic_backfill_done WHERE target_id=? AND request_revision=?',
            (int(target_id),int(request_revision))).fetchone())

    def mark_dynamic_backfill_done(self,target_id,request_revision,completed_at):
        self.db.execute('INSERT OR REPLACE INTO dynamic_backfill_done VALUES (?,?,?)',
                        (int(target_id),int(request_revision),str(completed_at)))
        self.db.commit()

    def explicit_backfill_done(self,group_id,chat_id):
        return bool(self.db.execute(
            'SELECT 1 FROM explicit_backfill_done WHERE group_id=? AND chat_id=?',
            (int(group_id),int(chat_id))).fetchone())

    def mark_explicit_backfill_done(self,group_id,chat_id,completed_at):
        self.db.execute('INSERT OR REPLACE INTO explicit_backfill_done VALUES (?,?,?)',
                        (int(group_id),int(chat_id),str(completed_at)))
        self.db.commit()



def assert_session_free(session_path):
    # Detect V5/V6 workers, which did not take our new lock. Read-only inspection.
    if Path('/usr/sbin/lsof').exists() and session_path.exists():
        r = subprocess.run(['/usr/sbin/lsof', '-t', str(session_path)], text=True, capture_output=True, timeout=10)
        pids = {x.strip() for x in r.stdout.splitlines() if x.strip().isdigit() and int(x.strip()) != os.getpid()}
        if pids:
            raise RuntimeError('Telegram-сессия уже занята PID ' + ', '.join(sorted(pids)) + '. Не запускайте второй сборщик.')


class Collector:
    def __init__(self, db, client, outbox):
        self.db, self.client, self.outbox = db, client, outbox
        self.config = None
        self.config_seen = 0.0
        self._async_resources = None
        self.last_error = None
        self.flush_error = None
        self.last_message_at = None
        self.last_success = False
        self.resolving = False
        self.resolver_task = None
        self.settings = dict(DEFAULTS, telegram_owner_sender_id=None, media_processing_enabled=False)
        self.settings_seen = 0.0
        self.media_busy = None
        self.media_last_error = None
        self.media_settings_error = None
        self.media_stats = {'processed': 0, 'uploaded_originals': 0, 'audio_memory_failures': 0, 'crash_quarantines': 0}
        self.media_hydrated = 0.0
        self.media_capabilities = {}
        self.analysis_settings = {'analysis_enabled':False}
        self.analysis_error = None
        self.analysis_last = None
        self.client_preferences = {'analysis_mode':'chat_review'}
        self.review_last = None
        self.review_error = None
        self.estimate_last = None
        self.jobs_last = None
        self.jobs_error = None
        self.dialog_candidates = []
        self.dialog_candidates_scanned_at = None
        self.dialog_discovery_seen = None
        self.dialog_discovery_error = None
        self.dialog_topic_index = {}
        self.dialog_forum_ids = set()
        self.dynamic_backfill_last = None
        self.dynamic_source_activation_last = None
        self.source_exclusions = None
        self.source_exclusions_seen = None
        self.source_exclusions_error = None
        self.discovery_rows = []
        self.postproduction_discovery = {
            'privacy_guard':'exclusions_before_content','candidates':[],'skipped':[],
            'excluded_content_exposed':False,'candidate_media_processed':False,
        }

    def _resources(self):
        # Python 3.9 binds primitives to the loop at construction. Constructors
        # used by metadata/preflight tests must not allocate an implicit loop.
        loop = asyncio.get_running_loop()
        if self._async_resources is None:
            self._async_resources = (loop, asyncio.Lock(), asyncio.Event())
        elif self._async_resources[0] is not loop:
            raise RuntimeError('Collector cannot be reused across event loops')
        return self._async_resources

    @property
    def db_lock(self):
        return self._resources()[1]

    @property
    def stop(self):
        return self._resources()[2]

    async def io(self, function):
        # One Supabase request at a time; synchronous SDK cannot block Telegram's loop.
        async with self.db_lock:
            return await asyncio.to_thread(function)

    async def refresh(self):
        current = await self.io(lambda: get_config(self.db))
        changed = self.config is None or current.revision != self.config.revision
        self.config, self.config_seen = current, time.monotonic()
        try:
            self.source_exclusions = await self.io(lambda: get_source_exclusions(self.db))
            self.source_exclusions_seen = time.monotonic()
            self.source_exclusions_error = None
        except Exception as exc:
            # Discovery, activation and backfill fail closed when privacy config is unavailable.
            self.source_exclusions = None
            self.source_exclusions_seen = None
            self.source_exclusions_error = safe_error(exc)
        try:
            rows = await self.io(lambda: self.db.table("tm_runtime_settings").select("*").eq("id", True).limit(1).execute().data)
            if not rows: raise MediaError('runtime_settings_missing')
            prefs=await self.io(lambda:self.db.table('tm_client_preferences').select('*').eq('instance_id','macbook-owner').limit(1).execute().data)
            self.client_preferences=prefs[0] if prefs else {'analysis_mode':'chat_review'}
            self.analysis_settings = dict(rows[0],analysis_mode=self.client_preferences.get('analysis_mode','chat_review'))
            self.settings = dict(policy(rows[0]), telegram_owner_sender_id=rows[0].get('telegram_owner_sender_id'))
            self.settings_seen = time.monotonic()
            self.media_settings_error = None
        except Exception as exc:
            # Invalid media settings disable only extraction, not text collection.
            self.settings['media_processing_enabled'] = False
            self.analysis_settings['analysis_enabled'] = False
            self.settings_seen = 0
            self.media_settings_error = safe_error(exc)
            print('MEDIA SETTINGS ERROR: ' + self.media_settings_error, flush=True)
        if changed:
            print('CONFIG V18: ' + json.dumps(current.summary(), ensure_ascii=False), flush=True)
        return current

    def allowed(self, cid):
        return bool(self.config and time.monotonic() - self.config_seen <= CONFIG_MAX_AGE and cid in self.config.whitelist)

    def _postproduction_only(self, cid):
        if not self.config:
            return False
        gids = self.config.by_chat.get(int(cid), [])
        return bool(gids) and all(self.config.groups[g].get('rules_profile') == 'postproduction' for g in gids)

    def allowed_message(self, cid, message):
        if not self.allowed(cid):
            return False
        if not self._postproduction_only(cid):
            return True
        if int(cid) not in self.dialog_forum_ids:
            return True
        reply = getattr(message, 'reply_to', None)
        candidates=[]
        if reply:
            candidates.extend([getattr(reply,'reply_to_top_id',None),getattr(reply,'reply_to_msg_id',None)])
        candidates.append(getattr(message,'id',None))
        for topic_id in candidates:
            if topic_id is None:
                continue
            title=self.dialog_topic_index.get((int(cid),int(topic_id)))
            if title is not None:
                return postproduction_topic_allowed(title)
        if reply is None:
            general=[title for (chat_id,_),title in self.dialog_topic_index.items()
                     if chat_id==int(cid) and postproduction_topic_kind(title)=='general']
            if len(general)==1:
                return True
        return False

    def serialize_message(self, m, sender_name):
        return {'chat_id': int(m.chat_id), 'message_id': int(m.id),
                'date': m.date.isoformat() if m.date else None,
                'sender_id': m.sender_id, 'sender_name': sender_name, 'text': m.message or '',
                'reply_to_message_id': getattr(m.reply_to, 'reply_to_msg_id', None) if m.reply_to else None,
                'has_media': bool(m.media), 'media_count': 1 if m.media else 0,
                'edited_at': m.edit_date.isoformat() if getattr(m, 'edit_date', None) else None,
                'is_deleted': False, 'deleted_at': None}

    def media_meta(self, m):
        return media_meta(m)

    async def on_message(self, event):
        cid = event.chat_id
        if not self.allowed(cid):
            return
        m = event.message
        if not self.allowed_message(cid, m):
            return
        sender = getattr(m, 'sender', None)
        if sender is None:
            try:
                sender = await asyncio.wait_for(m.get_sender(), timeout=10)
            except Exception:
                sender = None
        if not self.allowed(cid):
            return
        first, last = getattr(sender, 'first_name', '') or '', getattr(sender, 'last_name', '') or ''
        username = getattr(sender, 'username', None)
        sender_name = ' '.join(x for x in (first, last) if x).strip()
        if not sender_name:
            sender_name = getattr(sender, 'title', None) or ('@' + username if username else None)
        user = {'sender_id': m.sender_id, 'sender_name': sender_name, 'username': username} if m.sender_id else None
        message = self.serialize_message(m, sender_name)
        message['_attachment'] = self.media_meta(m)
        self.outbox.add(user, message)
        if not getattr(m, 'is_channel', False):
            self.outbox.db.execute('INSERT OR REPLACE INTO message_index VALUES (?,?)',(int(m.id),int(cid)))
            self.outbox.db.commit()
        self.last_message_at = now_iso()

    async def on_edited(self, event):
        if not self.allowed(event.chat_id):
            return
        await self.on_message(event)
        print('MESSAGE EDIT QUEUED: chat={} message={}'.format(event.chat_id, event.message.id), flush=True)

    async def on_deleted(self, event):
        ids = [int(x) for x in event.deleted_ids]
        pairs = []
        if event.chat_id is not None:
            if not self.allowed(event.chat_id): return
            pairs = [(int(event.chat_id), mid) for mid in ids]
        else:
            # Private/basic-group deletions lack a peer. Resolve only saved
            # account-wide IDs; never guess a channel from a colliding message ID.
            from telethon import utils, types
            nonchannels = [cid for cid in (self.config.whitelist if self.config else {})
                           if utils.resolve_id(cid)[1] != types.PeerChannel and self.allowed(cid)]
            for start in range(0,len(ids),100):
                batch=ids[start:start+100]
                if not nonchannels: break
                rows=await self.io(lambda b=batch: self.db.table('telegram_messages').select('chat_id,message_id').in_('chat_id',nonchannels).in_('message_id',b).execute().data)
                pairs.extend((int(r['chat_id']),int(r['message_id'])) for r in rows)
                for mid in batch:
                    rows=self.outbox.db.execute('SELECT chat_id FROM message_index WHERE message_id=?',(mid,)).fetchall()
                    pairs.extend((r[0],mid) for r in rows if r[0] in nonchannels)
        for cid,mid in set(pairs):
            if self.allowed(cid):
                self.outbox.add(None, {'chat_id':cid,'message_id':mid,'_delete_only':True,'deleted_at':now_iso()})
        self.last_message_at = now_iso()

    async def flush(self):
        for cid, mid, payload in self.outbox.rows():
            if not self.config or time.monotonic() - self.config_seen > CONFIG_MAX_AGE: return
            if cid not in self.config.whitelist:
                self.outbox.remove(cid, mid, payload)
                continue
            item = json.loads(payload)
            def persist():
                msg = dict(item['message'])
                delete_only = bool(msg.pop('_delete_only', False))
                attachment = msg.pop('_attachment', None)
                if item['user']:
                    self.db.table('telegram_users').upsert(item['user'], on_conflict='sender_id').execute()
                committed = self.db.rpc('tm_ingest_message_v10', {'p_message':msg,'p_attachment':attachment,'p_delete':delete_only}).execute().data
                if committed.get('applied') and not delete_only and msg.get('reply_to_message_id') and msg.get('sender_id'):
                    self.db.rpc('tm_register_reply_v10', {'p_chat_id':cid,'p_reply_message_id':mid}).execute()
                return committed
            await self.io(persist)
            self.outbox.remove(cid, mid, payload)
            print('MESSAGE SAVED: chat={} message={}'.format(cid, mid), flush=True)

    async def hydrate_content(self):
        if not self.config or not self.settings['media_processing_enabled']: return
        cids=[cid for cid in self.config.whitelist if self.allowed(cid)]
        for start in range(0,len(cids),100):
            rows=await self.io(lambda cs=cids[start:start+100]: self.db.table('telegram_attachments').select('chat_id,message_id,attachment_index,source_token,kind,mime_type,file_name,file_size,duration_seconds,width,height,retry_requested_at,processing_status').in_('chat_id',cs).eq('source_available',True).in_('processing_status',['pending','needs_setup','failed','partial']).order('updated_at',desc=True).limit(100).execute().data)
            for meta in rows:
                if meta.get('source_token') and self.allowed(int(meta['chat_id'])):
                    self.outbox.content.offer(meta, MEDIA_PROCESSOR_REVISION)

    async def processor(self, path, meta, settings):
        request=run_processor_request(path,meta,settings)
        safe_env=('HOME','PATH','TMPDIR','LANG','LC_ALL','SYSTEMROOT',
                  'TM_MEDIA_BACKEND','TM_OCR_LANG','TM_MODELS_HOME','TM_MODELS_DIR','TM_AUDIO_PYTHON',
                  'TM_OPENAI_VISION_ENABLED','TM_OPENAI_VISION_MODEL','TM_OPENAI_VISION_API_KEY')
        env={k:v for k,v in os.environ.items() if k in safe_env}
        if env.get('TM_OPENAI_VISION_ENABLED','').strip().lower() not in ('1','true','yes','on'):
            env.pop('TM_OPENAI_VISION_API_KEY',None)
        env.update(HF_HUB_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='2')
        child=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name('tm_extract.py')),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            start_new_session=True,env=env)
        try:
            out,err=await asyncio.wait_for(child.communicate(json.dumps(request).encode()),timeout=settings['media_job_timeout_seconds']+10)
            if len(out)>3000000: raise MediaError('processor_output_limit')
            return normalize_result(json.loads(out),settings['media_max_text_chars'])
        except BaseException:
            # Kill the full process group, including local ASR, before removing its input.
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await child.wait()
            raise

    async def process_content(self, job):
        meta=job['metadata']; cid=job['chat_id']; mid=job['message_id']
        if not self.allowed(cid) or not self.settings['media_processing_enabled']:
            self.outbox.content.defer(job); return
        if not self.outbox.content.same(job): return
        result=job.get('result')
        if result is None:
            job=self.outbox.content.begin(job)
            if job is None:
                return
            result=job.get('result')
        if result is None:
            settings=policy(self.settings)
            reason=skip_reason(meta,settings)
            if reason:
                result=blank_result('skipped',reason)
            else:
                from tm_extract import capabilities
                caps=await asyncio.to_thread(capabilities,settings)
                self.media_capabilities=caps
                if meta['kind'] in ('audio','voice') and not caps['local_audio_model_files_present']:
                    result=blank_result('needs_setup','local_audio_model_missing')
                elif meta['kind'] in ('photo','pdf') and not caps['native_pdf_and_ocr']:
                    result=blank_result('needs_setup','native_bridge_missing')
                else:
                    try:
                        current=await asyncio.wait_for(self.client.get_messages(cid,ids=mid),timeout=30)
                        if not current or not getattr(current,'media',None):
                            result=blank_result('source_unavailable','telegram_source_unavailable')
                        elif (self.media_meta(current) or {}).get('source_token')!=job['source_token']:
                            await self.on_message(SimpleNamespace(chat_id=cid,message=current))
                            self.outbox.content.finish(job); return
                        elif not self.allowed(cid):
                            self.outbox.content.defer(job); return
                        else:
                            with TempMedia() as tempdir:
                                path=tempdir/'original.bin'
                                with path.open('xb') as f:
                                    os.chmod(path,0o600)
                                    stream=LimitedWriter(f,settings['media_max_bytes'])
                                    await asyncio.wait_for(current.download_media(file=stream),timeout=120)
                                    stream.flush()
                                if not self.allowed(cid):
                                    self.outbox.content.defer(job); return
                                result=await self.processor(path,meta,settings)
                            # Original and normalized preview are removed before DB persistence.
                    except asyncio.CancelledError:
                        self.outbox.content.defer(job,30)
                        raise
                    except asyncio.TimeoutError:
                        result=blank_result('failed','download_or_processor_timeout')
                    except Exception as exc:
                        result=blank_result('failed',str(exc) if isinstance(exc,MediaError) else type(exc).__name__)
            self.outbox.content.result(job,result)
        if not self.allowed(cid) or not self.settings['media_processing_enabled']:
            self.outbox.content.defer(job); return
        reply=await self.io(lambda: self.db.rpc('tm_commit_attachment_text_v10',{
            'p_chat_id':cid,'p_message_id':mid,'p_source_token':job['source_token'],'p_result':result}).execute().data)
        if not reply.get('applied'):
            if reply.get('reason')=='chat_disabled': self.outbox.content.defer(job,60)
            else: self.outbox.content.finish(job)
            return
        error=result.get('processing_error')
        memory_failure=error in ('audio_process_sigkill','audio_memory_error')
        crash_exhausted=error=='processing_crash_retry_exhausted'
        if memory_failure:
            self.media_stats['audio_memory_failures']+=1
        if crash_exhausted:
            self.media_stats['crash_quarantines']+=1
        retry=result['processing_status'] in ('needs_setup','failed') or (
            result['processing_status']=='partial' and result.get('processing_details',{}).get('retry_for_description'))
        if crash_exhausted or (memory_failure and int(job.get('attempts') or 0)>=2):
            retry=False
        self.outbox.content.finish(job,retry=retry)
        self.media_stats['processed']+=1
        print('CONTENT TEXT SAVED: chat={} message={} status={}'.format(cid,mid,result['processing_status']),flush=True)

    async def content_loop(self):
        TempMedia().sweep()
        while not self.stop.is_set():
            job=None
            try:
                if self.settings['media_processing_enabled'] and time.monotonic()-self.settings_seen<=CONFIG_MAX_AGE:
                    if time.monotonic()-self.media_hydrated>=30:
                        await self.hydrate_content(); self.media_hydrated=time.monotonic()
                    allowed={cid for cid in (self.config.whitelist if self.config else {}) if self.allowed(cid)}
                    job=self.outbox.content.peek(allowed)
                    if job:
                        self.media_busy={'chat_id':job['chat_id'],'message_id':job['message_id']}
                        await self.process_content(job)
                self.media_last_error=None
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.media_last_error=safe_error(exc)
                if job: self.outbox.content.defer(job,60)
                print('CONTENT WORKER ERROR: '+self.media_last_error,flush=True)
            finally:
                self.media_busy=None
            try: await asyncio.wait_for(self.stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass

    def _source_excluded(self, chat_id, name):
        if self.source_exclusions is None:
            return True
        return bool(excluded_reason(
            int(chat_id), name or '',
            self.source_exclusions.get('excluded_names'),
            self.source_exclusions.get('excluded_chat_ids')))

    async def refresh_dialog_candidates(self):
        if self.source_exclusions is None:
            raise RuntimeError('source_exclusions_unavailable')
        rows, topic_index, skipped = await asyncio.wait_for(
            read_candidates(
                self.client, DIALOG_DISCOVERY_SCAN_LIMIT,
                excluded_names=self.source_exclusions.get('excluded_names'),
                excluded_chat_ids=self.source_exclusions.get('excluded_chat_ids')),
            timeout=120)
        self.discovery_rows = rows
        self.postproduction_discovery = discovery_bundle(rows, skipped)
        self.dialog_candidates = publish_candidates(rows, DIALOG_DISCOVERY_PUBLISH_LIMIT)
        self.dialog_topic_index = topic_index
        self.dialog_forum_ids = {int(row['chat_id']) for row in rows if row.get('forum')}
        self.dialog_candidates_scanned_at = now_iso()
        self.dialog_discovery_seen = time.monotonic()
        self.dialog_discovery_error = None

    async def sync_postproduction_sources(self, max_activations=1):
        """Guarded deterministic source activation; no LLM or ambiguous activation."""
        if self.source_exclusions is None or not self.config:
            return 0
        groups=[g for g in self.config.monitored_groups()
                if g.get('rules_profile')=='postproduction']
        if len(groups)!=1:
            self.dynamic_source_activation_last={
                'status':'blocked','reason':'postproduction_group_not_unique','activated':0}
            return 0
        group=groups[0]
        gid=int(group['id'])
        active_targets=self.config.raw.get('targets',[])
        activated=0
        for row in activation_candidates(self.discovery_rows):
            cid=int(row['chat_id'])
            name=row.get('name') or ''
            if self._source_excluded(cid,name):
                continue
            if gid in self.config.by_chat.get(cid,[]):
                continue
            if any(t.get('enabled') and int(t.get('group_id') or 0)==gid
                   and int(t.get('chosen_chat_id') or t.get('resolved_chat_id') or 0)==cid
                   for t in active_targets):
                continue
            classification=row.get('_classification') or {}
            if classification.get('state') not in ('post_started','waiting_materials'):
                continue
            payload={
                'group_id':gid,
                'selector_kind':'title',
                'selector_value':'dynamic:'+str(cid),
                'display_name':name[:200],
                'chosen_chat_id':cid,
                'enabled':True,
            }
            await self.io(lambda p=payload: self.db.table('telegram_chat_group_targets').upsert(
                p,on_conflict='group_id,selector_kind,selector_value').execute().data)
            activated+=1
            self.dynamic_source_activation_last={
                'status':'requested','chat_id':cid,'kind':row.get('kind'),
                'classification':classification.get('state'),
                'signals':list(classification.get('signals') or []),
                'activated':activated,'at':now_iso(),
            }
            if activated>=max_activations:
                break
        if activated:
            await self.refresh()
        return activated

    async def read_dialog_metadata(self):
        if self.source_exclusions is None:
            raise RuntimeError('source_exclusions_unavailable')
        rows = []
        async for dialog in self.client.iter_dialogs():
            cid=int(dialog.id)
            name=dialog.name or ''
            if self._source_excluded(cid,name):
                continue
            entity = dialog.entity
            usernames = []
            if getattr(entity, 'username', None):
                usernames.append(entity.username)
            usernames += [u.username for u in (getattr(entity, 'usernames', None) or [])
                          if getattr(u, 'active', False)]
            rows.append({'chat_id': cid, 'name': name, 'usernames': usernames})
        # The complete non-excluded directory stays only in RAM; only actual matches are saved.
        return rows

    def _pending_postproduction_backfills(self):
        if not self.config or self.source_exclusions is None:
            return []
        pending=[]
        seen=set()
        for target in self.config.raw.get('targets',[]):
            if not target.get('enabled') or target.get('resolution_status')!='resolved':
                continue
            cid=target.get('chosen_chat_id') or target.get('resolved_chat_id')
            try:
                cid=int(cid)
                gid=int(target['group_id'])
                ident=int(target['id'])
                revision=int(target.get('request_revision') or 0)
            except (TypeError,ValueError,KeyError):
                continue
            group=self.config.groups.get(gid)
            if not group or group.get('rules_profile')!='postproduction':
                continue
            chat_name=(self.config.chats.get(cid) or {}).get('chat_name','')
            if self._source_excluded(cid,chat_name):
                continue
            if self.outbox.dynamic_backfill_done(ident,revision):
                continue
            seen.add((gid,cid))
            pending.append({'source_kind':'target','target_id':ident,'revision':revision,
                            'group_id':gid,'chat_id':cid})
        for member in self.config.raw.get('members',[]):
            if not member.get('enabled',True) or member.get('managed_by_targets'):
                continue
            try:
                gid=int(member['group_id'])
                cid=int(member['chat_id'])
            except (TypeError,ValueError,KeyError):
                continue
            group=self.config.groups.get(gid)
            if not group or group.get('rules_profile')!='postproduction' or (gid,cid) in seen:
                continue
            chat_name=(self.config.chats.get(cid) or {}).get('chat_name','')
            if self._source_excluded(cid,chat_name):
                continue
            if gid not in self.config.by_chat.get(cid,[]) or self.outbox.explicit_backfill_done(gid,cid):
                continue
            pending.append({'source_kind':'member','group_id':gid,'chat_id':cid})
        return sorted(pending,key=lambda x:(x['chat_id'],x['group_id'],x['source_kind']))

    async def hydrate_postproduction_sources(self,max_sources=1,message_limit=40):
        """One-time bounded history hydrate after a Postproduction source is selected.

        Candidate discovery remains metadata-only. History is read only after the
        refreshed config authorizes the exact source through a resolved selector or
        explicit workspace membership. Progress is durable in the worker SQLite
        state; queued messages are also durable/idempotent there.
        """
        for source in self._pending_postproduction_backfills()[:max_sources]:
            gid=source['group_id']
            cid=source['chat_id']
            chat_name=(self.config.chats.get(cid) or {}).get('chat_name','')
            if self._source_excluded(cid,chat_name):
                continue
            if not self.allowed(cid):
                continue
            if gid not in self.config.by_chat.get(cid,[]):
                continue
            # Privacy guard is checked again immediately before any history read.
            if self._source_excluded(cid,chat_name):
                continue
            messages=list(await asyncio.wait_for(self.client.get_messages(cid,limit=message_limit),timeout=60))
            queued=0
            for message in reversed(messages):
                if not self.allowed(cid):
                    return
                if not self.allowed_message(cid,message):
                    continue
                await self.on_message(SimpleNamespace(chat_id=cid,message=message))
                queued+=1
            if self.allowed(cid):
                completed=now_iso()
                if source['source_kind']=='target':
                    self.outbox.mark_dynamic_backfill_done(source['target_id'],source['revision'],completed)
                else:
                    self.outbox.mark_explicit_backfill_done(gid,cid,completed)
                self.dynamic_backfill_last={'source_kind':source['source_kind'],'group_id':gid,'chat_id':cid,
                                            'queued':queued,'scanned':len(messages),'completed_at':completed}
                print('POSTPRODUCTION BACKFILL: kind={} group={} chat={} queued={} scanned={}'.format(
                    source['source_kind'],gid,cid,queued,len(messages)),flush=True)

    async def resolve(self):
        pending = self.config.pending_targets() if self.config else []
        if not pending:
            return
        self.resolving = True
        print('RESOLVE V18: {} новых запросов; только метаданные диалогов'.format(len(pending)), flush=True)
        try:
            try:
                dialogs = await asyncio.wait_for(self.read_dialog_metadata(), timeout=90)
                error = None
            except Exception as exc:
                dialogs, error = [], safe_error(exc)
                delay = max(900, int(getattr(exc, 'seconds', 0)) + 5)
            for target in pending:
                if self.stop.is_set():
                    break
                result, matches = ('error', []) if error else match_target(target, dialogs)
                selected = matches[0] if result == 'resolved' else None
                candidates = [{'chat_id': d['chat_id'], 'name': d['name'],
                               'username': (d['usernames'][0] if d['usernames'] else None)} for d in matches[:20]]
                params = {'p_target_id': target['id'], 'p_revision': target['request_revision'],
                          'p_status': result, 'p_chat_id': selected['chat_id'] if selected else None,
                          'p_chat_name': (selected['name'] or target.get('display_name') or target['selector_value']) if selected else None,
                          'p_candidates': candidates, 'p_error': error,
                          'p_retry_seconds': delay if error else 900}
                applied = await self.io(lambda p=params: self.db.rpc('rr_commit_group_target_v7', p).execute().data)
                print('TARGET {}: {}'.format(target['id'], applied.get('status') if applied.get('applied') else 'STALE/DISABLED'), flush=True)
            await self.refresh()
        finally:
            self.resolving = False

    async def health(self):
        details = self.config.summary() if self.config else {}
        details.update({'connected': self.client.is_connected(), 'outbox_pending': self.outbox.count(),
                        'last_message_at': self.last_message_at, 'resolving': self.resolving,
                        'content_pending': self.outbox.content.count(), 'refresh_seconds': REFRESH_SECONDS,
                        'media_mode': 'text_only', 'media_busy': self.media_busy,
                        'media_last_error': self.media_last_error, 'media_settings_error': self.media_settings_error, 'media_capabilities': self.media_capabilities,
                        'media_stats': self.media_stats, 'memory': cgroup_memory_stats(),
                        'dialog_candidates': self.dialog_candidates,
                        'dialog_candidates_scanned_at': self.dialog_candidates_scanned_at,
                        'dynamic_backfill_last': self.dynamic_backfill_last,
                        'dynamic_backfill_pending': len(self._pending_postproduction_backfills()) if self.config else 0,
                        'dynamic_source_activation_last': self.dynamic_source_activation_last,
                        'postproduction_discovery': self.postproduction_discovery,
                        'source_exclusion_guard': {
                            'available': self.source_exclusions is not None,
                            'excluded_name_count': len(self.source_exclusions.get('excluded_names',[])) if self.source_exclusions else 0,
                            'excluded_chat_id_count': len(self.source_exclusions.get('excluded_chat_ids',[])) if self.source_exclusions else 0,
                            'error': self.source_exclusions_error,
                            'applied_before_content': True,
                        },
                        'dialog_discovery_error': self.dialog_discovery_error,
                        'analysis_error':self.analysis_error, 'analysis_last':self.analysis_last, 'jobs_last':self.jobs_last, 'jobs_error':self.jobs_error, 'review_last':self.review_last, 'review_error':self.review_error, 'estimate_last':self.estimate_last})
        error = (self.last_error or self.flush_error or self.jobs_error or self.review_error
                 or self.media_settings_error or self.source_exclusions_error)
        okay = bool(self.config and self.client.is_connected() and not error)
        await self.io(lambda: save_health(self.db, 'telegram-collector', 'running' if okay else 'degraded',
                                         details, success=okay, error=error))

    async def maintenance(self):
        while not self.stop.is_set():
            try:
                await self.refresh()
                if discovery_due(self.dialog_discovery_seen, time.monotonic(), DIALOG_DISCOVERY_SECONDS):
                    try:
                        await self.refresh_dialog_candidates()
                        await self.sync_postproduction_sources(max_activations=1)
                    except Exception as exc:
                        self.dialog_discovery_error = safe_error(exc)
                        self.dialog_discovery_seen = time.monotonic()
                        print('DIALOG DISCOVERY ERROR: ' + self.dialog_discovery_error, flush=True)
                # Resolution runs only after guarded discovery/activation. It is still asynchronous
                # so a slow metadata lookup cannot stall the heartbeat.
                if self.resolver_task and self.resolver_task.done():
                    try:
                        self.resolver_task.result()
                    except Exception as exc:
                        print('RESOLVER ERROR: ' + safe_error(exc), flush=True)
                    self.resolver_task = None
                if not self.resolver_task and self.config.pending_targets():
                    self.resolver_task = asyncio.create_task(self.resolve())
                try:
                    await self.hydrate_postproduction_sources(max_sources=1,message_limit=40)
                except Exception as exc:
                    self.dialog_discovery_error = safe_error(exc)
                    print('POSTPRODUCTION BACKFILL ERROR: ' + self.dialog_discovery_error, flush=True)
                self.last_error = None
                await self.health()
            except Exception as exc:
                self.last_error = safe_error(exc)
                print('CONFIG/HEALTH ERROR: ' + self.last_error, flush=True)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=REFRESH_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def flush_loop(self):
        while not self.stop.is_set():
            try:
                await self.flush()
                self.flush_error = None
            except Exception as exc:
                self.flush_error = safe_error(exc)
                print('OUTBOX ERROR: ' + self.flush_error, flush=True)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


def migrate_text_outbox(outbox):
    outbox.db.execute('CREATE TABLE IF NOT EXISTS legacy_imports (name TEXT PRIMARY KEY)')
    for version in ('V15','V14','V13','V12','V11','V10','V9','V8','V7'):
        name='telegram_outbox_'+version+'.sqlite'; src=APP_DIR/name
        if not src.is_file() or src.is_symlink(): continue
        if outbox.db.execute('SELECT 1 FROM legacy_imports WHERE name=?',(name,)).fetchone(): break
        old=sqlite3.connect(src.as_uri()+'?mode=ro',uri=True)
        try:
            if version=='V9' and old.execute("SELECT name FROM sqlite_master WHERE name='media_pending'").fetchone():
                if old.execute('SELECT count(*) FROM media_pending').fetchone()[0]:
                    raise RuntimeError('V9 содержит старую очередь файлов; требуется отдельная проверка миграции')
            rows=old.execute('SELECT chat_id,message_id,payload FROM pending').fetchall()
            for cid,mid,payload in rows:
                outbox.db.execute('INSERT OR IGNORE INTO pending VALUES (?,?,?)',(cid,mid,payload))
            if old.execute("SELECT name FROM sqlite_master WHERE name='content_jobs'").fetchone():
                fields='chat_id,message_id,source_token,metadata,retry_stamp,state,attempts,next_attempt,result'
                for row in old.execute('SELECT '+fields+' FROM content_jobs').fetchall():
                    outbox.db.execute('INSERT OR IGNORE INTO content_jobs('+fields+') VALUES (?,?,?,?,?,?,?,?,?)',row)
            if old.execute("SELECT name FROM sqlite_master WHERE name='message_index'").fetchone():
                for row in old.execute('SELECT message_id,chat_id FROM message_index').fetchall():
                    outbox.db.execute('INSERT OR IGNORE INTO message_index(message_id,chat_id) VALUES (?,?)',row)
            outbox.db.execute('INSERT INTO legacy_imports VALUES (?)',(name,))
            outbox.db.commit()
        finally: old.close()
        # Each version migrated its predecessors; older queues are historical snapshots.
        break


async def serve(env):
    from telethon import TelegramClient, events
    from tm_telegram_transport import telethon_proxy
    # Created, connected, used and disconnected within one asyncio.run invocation.
    handle = lock_file('telegram_collector.lock')
    session = Path(os.environ.get('TM_TELEGRAM_SESSION', str(Path.home() / 'telegram_helper.session')))
    if not session.is_file():
        raise RuntimeError('Нет существующей Telegram-сессии; V18 не запрашивает код входа автоматически')
    assert_session_free(session)
    db = database(env)
    from tm_contract import check_contract
    check_contract(db)
    client = TelegramClient(str(session.with_suffix('')), int(env['TELEGRAM_API_ID']), env['TELEGRAM_API_HASH'],
                            timeout=10, connection_retries=3, request_retries=2, retry_delay=3,
                            flood_sleep_threshold=0, catch_up=True, proxy=telethon_proxy())
    outbox = Outbox(APP_DIR / 'telegram_outbox_V16.sqlite')
    migrate_text_outbox(outbox)
    worker = Collector(db, client, outbox)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop.set)
    tasks = []
    try:
        print('Telegram collector V18: подключение...', flush=True)
        await worker.refresh()  # No Telegram data is read before an authorized config.
        client.add_event_handler(worker.on_message, events.NewMessage())
        client.add_event_handler(worker.on_edited, events.MessageEdited())
        client.add_event_handler(worker.on_deleted, events.MessageDeleted())
        await asyncio.wait_for(client.connect(), timeout=40)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=20):
            raise RuntimeError('Telegram-сессия требует повторного входа; автоматический вход отключён')
        print('TELEGRAM V18 CONNECTED; конфигурация только из Supabase', flush=True)
        from tm_jobs import jobs_loop, analysis_loop
        from tm_background import review_loop
        tasks = [asyncio.create_task(worker.maintenance()), asyncio.create_task(worker.flush_loop()), asyncio.create_task(worker.content_loop()), asyncio.create_task(jobs_loop(worker)), asyncio.create_task(analysis_loop(worker)), asyncio.create_task(review_loop(worker))]
        background = list(tasks)
        stopped = asyncio.create_task(worker.stop.wait())
        tasks.append(stopped)
        disconnected = client.disconnected
        done, _ = await asyncio.wait([stopped, disconnected] + background, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            disconnected.result()
            if not worker.stop.is_set():
                raise RuntimeError('Telegram отключился; launchd перезапустит сборщик')
        for t in done:
            if t in background:
                t.result()
                raise RuntimeError('Фоновый цикл неожиданно остановлен')
    finally:
        worker.stop.set()
        if worker.resolver_task:
            tasks.append(worker.resolver_task)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            pass
        try:
            pending_count = outbox.count()
            await worker.io(lambda: save_health(db, 'telegram-collector', 'stopped', {'outbox_pending': pending_count}))
        except Exception:
            pass
        outbox.db.close()
        handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Читать только конфигурацию Supabase, без Telegram и записи')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    env = load_env()
    if args.check:
        cfg = get_config(database(env))
        print('=== TELEGRAM V18 CONFIG CHECK (READ ONLY) ===', flush=True)
        print(json.dumps(cfg.summary(), ensure_ascii=False, indent=2), flush=True)
        for g in cfg.monitored_groups():
            print('GROUP {} → {}'.format(g['group_key'], g.get('reminder_list_name')), flush=True)
            for cid, ids in sorted(cfg.by_chat.items()):
                if g['id'] in ids:
                    print('  {}: {}'.format(cid, cfg.whitelist[cid]), flush=True)
        print('CHECK V18 OK; Telegram-сессия не открывалась; запись отключена.', flush=True)
        return
    from rr_common import wait_for_deployment
    wait_for_deployment()
    asyncio.run(serve(env))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print('FATAL V18: ' + safe_error(exc), flush=True)
        raise SystemExit(1)
