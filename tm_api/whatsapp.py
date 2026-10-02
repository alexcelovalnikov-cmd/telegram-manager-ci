"""Read-only self-hosted WhatsApp history backed by the server SQLite store."""
from __future__ import annotations
import hashlib
import json
import re
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timezone
from .v24.common import Rejected, digest, make_cursor, read_cursor

def _norm(value: str) -> str:
    return " ".join((value or "").casefold().replace("ё","е").split())

class WhatsAppReader:
    def __init__(self, path: str, cursor_secret: str):
        self.path=Path(path)
        self.cursor_secret=cursor_secret
        self.backfill_request_path=self.path.parent/'backfill-request.json'
        self.backfill_result_path=self.path.parent/'backfill-result.json'

    @staticmethod
    def _json_file(path: Path):
        if not path.exists():
            return None
        if path.is_symlink():
            raise Rejected('whatsapp_backfill_state_invalid')
        try:
            if path.stat().st_size > 65536:
                raise Rejected('whatsapp_backfill_state_invalid')
            value=json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            raise Rejected('whatsapp_backfill_state_invalid')
        return value if isinstance(value,dict) else None

    @staticmethod
    def _atomic_json(path: Path, value: dict):
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=str(path.parent))
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w') as handle:
                handle.write(json.dumps(value,ensure_ascii=False,separators=(',',':')))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp,path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _db(self):
        if not self.path.is_absolute():
            raise Rejected('whatsapp_store_path_invalid')
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise Rejected('whatsapp_store_path_invalid')
        db=sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True,timeout=5)
        db.row_factory=sqlite3.Row
        return db

    @staticmethod
    def _row(row):
        if row is None:return None
        value=dict(row)
        value['from_me']=bool(value.get('from_me'))
        value['has_media']=bool(value.get('has_media'))
        return value

    @staticmethod
    def _table_exists(db,name):
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)
        ).fetchone() is not None

    @staticmethod
    def _reaction_row(row):
        value=dict(row)
        value['is_removed']=bool(value.get('is_removed'))
        return value

    def _transport_status(self, meta=None):
        meta=meta or {}
        base={'transport_bootstrap':'unknown','transport_bootstrap_percent':None,'transport_error':None}
        if meta.get('transport_topology')=='shared_worker_tor':
            if meta.get('connection_status')=='connected' or meta.get('qr_ready')=='true':
                return {'transport_bootstrap':'ready','transport_bootstrap_percent':100,'transport_error':None}
            reason=meta.get('last_disconnect_reason') or ''
            if 'ECONNREFUSED 127.0.0.1:9050' in reason:
                return {
                    'transport_bootstrap':'pending_or_failed',
                    'transport_bootstrap_percent':None,
                    'transport_error':'shared Tor SOCKS endpoint unavailable',
                }
            return {'transport_bootstrap':'pending_or_failed','transport_bootstrap_percent':None,'transport_error':None}

        # Legacy V93-V96 dedicated-Tor diagnostic fallback.
        log=self.path.parent/'tor-bootstrap.log'
        if not log.exists() or log.is_symlink():
            return base
        try:
            size=log.stat().st_size
            with log.open('rb') as handle:
                handle.seek(max(0,size-65536))
                text=handle.read(65536).decode('utf-8','replace')
        except OSError:
            return base
        matches=re.findall(r'Bootstrapped (\d+)%',text)
        percent=int(matches[-1]) if matches else None
        errors=[
            line.strip()[:300] for line in text.splitlines()
            if any(marker in line for marker in (
                'Problem bootstrapping','Network is unreachable','Connection refused','timed out'
            ))
        ]
        return {
            'transport_bootstrap':'ready' if percent==100 else 'pending_or_failed',
            'transport_bootstrap_percent':percent,
            'transport_error':errors[-1] if errors else None,
        }

    def status(self):
        db=self._db()
        if db is None:
            return {'status':'not_linked','connected':False,'qr_ready':False,
                    'storage':'server_sqlite','read_only_source':True,**self._transport_status()}
        try:
            meta={r['key']:r['value'] for r in db.execute('SELECT key,value FROM meta')}
            chats=db.execute('SELECT count(*) AS n FROM chats').fetchone()['n']
            messages=db.execute('SELECT count(*) AS n FROM messages WHERE is_deleted=0').fetchone()['n']
            backfill=self._json_file(self.backfill_result_path)
            pending=self._json_file(self.backfill_request_path)
            if pending and (not backfill or pending.get('request_id')!=backfill.get('request_id')):
                backfill={
                    'request_id':pending.get('request_id'),
                    'jid':pending.get('jid'),
                    'count':pending.get('count'),
                    'status':'queued',
                    'created_at':pending.get('created_at'),
                }
            reactions=0
            identities=0
            if self._table_exists(db,'reaction_events'):
                reactions=db.execute('SELECT count(*) AS n FROM reaction_events').fetchone()['n']
            if self._table_exists(db,'identities'):
                identities=db.execute('SELECT count(*) AS n FROM identities').fetchone()['n']
            return {
                'status':meta.get('connection_status','unknown'),
                'connected':meta.get('connection_status')=='connected',
                'qr_ready':meta.get('qr_ready')=='true',
                'account_jid':meta.get('account_jid'),
                'last_event_at':meta.get('last_event_at'),
                'last_success_at':meta.get('last_success_at'),
                'history_synced_at':meta.get('history_synced_at'),
                'last_disconnect_code':meta.get('last_disconnect_code'),
                'last_disconnect_reason':meta.get('last_disconnect_reason'),
                'transport':'tor_socks' if meta.get('transport')=='tor_socks' else 'direct',
                'transport_topology':meta.get('transport_topology'),
                'chats':chats,'messages':messages,'reactions':reactions,'identities':identities,
                'storage':'server_sqlite','read_only_source':True,
                'history_backfill':backfill,
                **self._transport_status(meta),
            }
        finally:db.close()

    def plan_history_backfill(self,jid,count=50):
        if not isinstance(jid,str) or not 1<=len(jid)<=200:
            raise Rejected('whatsapp_chat_id_invalid')
        if type(count) is not int or not 1<=count<=50:
            raise Rejected('whatsapp_backfill_count_invalid')
        db=self._db()
        if db is None:
            raise Rejected('whatsapp_not_linked')
        try:
            meta={r['key']:r['value'] for r in db.execute('SELECT key,value FROM meta')}
            if meta.get('connection_status')!='connected':
                raise Rejected('whatsapp_not_connected')
            chat=db.execute('SELECT jid,name FROM chats WHERE jid=?',(jid,)).fetchone()
            if not chat:
                raise Rejected('whatsapp_chat_not_found')
            oldest=db.execute(
                'SELECT message_id,date,sender_jid,from_me FROM messages '
                'WHERE jid=? AND is_deleted=0 ORDER BY date ASC,seq ASC LIMIT 1',(jid,)
            ).fetchone()
            if not oldest:
                raise Rejected('whatsapp_chat_history_empty')
            pending=self._json_file(self.backfill_request_path)
            result=self._json_file(self.backfill_result_path)
            if pending and (not result or pending.get('request_id')!=result.get('request_id')
                            or result.get('status') in ('queued','requested')):
                raise Rejected('whatsapp_backfill_already_pending')
            return {
                'jid':jid,
                'chat_name':chat['name'],
                'count':count,
                'anchor_message_id':oldest['message_id'],
                'anchor_date':oldest['date'],
            }
        finally:
            db.close()

    def enqueue_history_backfill(self,request_id,jid,count,anchor_message_id,anchor_date):
        if not isinstance(request_id,str) or not request_id:
            raise Rejected('whatsapp_backfill_request_id_invalid')
        result=self._json_file(self.backfill_result_path)
        if result and result.get('request_id')==request_id:
            return {'accepted':True,'idempotent':True,**result}
        pending=self._json_file(self.backfill_request_path)
        if pending and pending.get('request_id')==request_id:
            return {
                'accepted':True,'idempotent':True,'request_id':request_id,
                'jid':jid,'count':count,'status':'queued',
                'anchor_message_id':anchor_message_id,'anchor_date':anchor_date,
            }
        info=self.plan_history_backfill(jid,count)
        if info['anchor_message_id']!=anchor_message_id or info['anchor_date']!=anchor_date:
            raise Rejected('whatsapp_backfill_anchor_changed')
        value={
            'request_id':request_id,
            'jid':jid,
            'count':count,
            'created_at':datetime.now(timezone.utc).isoformat(),
            'anchor_message_id':anchor_message_id,
            'anchor_date':anchor_date,
        }
        self._atomic_json(self.backfill_request_path,value)
        return {
            'accepted':True,
            'idempotent':False,
            'request_id':request_id,
            'jid':jid,
            'chat_name':info['chat_name'],
            'count':count,
            'anchor_message_id':anchor_message_id,
            'anchor_date':anchor_date,
            'status':'queued',
            'note':'Result is reported by get_whatsapp_status.history_backfill.',
        }

    def request_history_backfill(self,jid,count=50):
        info=self.plan_history_backfill(jid,count)
        return self.enqueue_history_backfill(
            str(uuid.uuid4()),info['jid'],info['count'],
            info['anchor_message_id'],info['anchor_date'])

    def chats(self, limit=50, query=''):
        if not 1<=limit<=100 or len(query)>300:raise Rejected('invalid_page')
        db=self._db()
        if db is None:return {'items':[],'status':'not_linked'}
        try:
            args=[];where=[]
            if query.strip():
                where.append('search_name LIKE ?');args.append('%'+_norm(query)+'%')
            rows=db.execute(
                'SELECT jid,name,kind,last_message_at,updated_at FROM chats '+
                (('WHERE '+' AND '.join(where)+' ') if where else '')+
                'ORDER BY coalesce(last_message_at,updated_at) DESC,jid LIMIT ?',(*args,limit)
            ).fetchall()
            return {'items':[dict(r) for r in rows],'status':'ok','source_content_is_untrusted':True}
        finally:db.close()

    def _message(self,db,jid,message_id):
        row=db.execute('SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                       'FROM messages WHERE jid=? AND message_id=? AND is_deleted=0',(jid,message_id)).fetchone()
        if not row:raise Rejected('whatsapp_message_not_found')
        return self._row(row)

    def _reaction_bundle(self,db,jid,message_id,limit=50):
        if not self._table_exists(db,'reaction_events'):
            return {
                'events':[],'current':[],'events_truncated':False,'current_truncated':False,
                'reaction_state_token':hashlib.sha256(b'[]').hexdigest(),
                'history_complete':False,'source_content_is_untrusted':True,
            }
        rows=db.execute(
            """SELECT re.seq,re.jid,re.target_message_id,re.reaction_message_id,
                      re.actor_identity_key,
                      coalesce(re.actor_jid,(
                        SELECT a.alias FROM identity_aliases a
                        WHERE a.identity_key=re.actor_identity_key AND a.kind='jid'
                        ORDER BY a.alias LIMIT 1
                      )) AS actor_jid,
                      coalesce(re.actor_lid,(
                        SELECT a.alias FROM identity_aliases a
                        WHERE a.identity_key=re.actor_identity_key AND a.kind='lid'
                        ORDER BY a.alias LIMIT 1
                      )) AS actor_lid,
                      coalesce(nullif(i.display_name,''),re.actor_display_name,'') AS actor_display_name,
                      re.reaction_value,re.is_removed,re.reacted_at,re.source,re.evidence_token
               FROM reaction_events re
               LEFT JOIN identities i ON i.identity_key=re.actor_identity_key
               WHERE re.jid=? AND re.target_message_id=?
               ORDER BY re.reacted_at DESC,re.seq DESC LIMIT ?""",
            (jid,message_id,limit+1)
        ).fetchall()
        events_truncated=len(rows)>limit
        events=[self._reaction_row(r) for r in rows[:limit]]
        current_rows=db.execute(
            """WITH ranked AS (
                 SELECT re.*,
                        row_number() OVER (
                          PARTITION BY re.actor_identity_key
                          ORDER BY re.reacted_at DESC,re.seq DESC
                        ) AS rn
                 FROM reaction_events re
                 WHERE re.jid=? AND re.target_message_id=?
               )
               SELECT r.seq,r.jid,r.target_message_id,r.reaction_message_id,
                      r.actor_identity_key,
                      coalesce(r.actor_jid,(
                        SELECT a.alias FROM identity_aliases a
                        WHERE a.identity_key=r.actor_identity_key AND a.kind='jid'
                        ORDER BY a.alias LIMIT 1
                      )) AS actor_jid,
                      coalesce(r.actor_lid,(
                        SELECT a.alias FROM identity_aliases a
                        WHERE a.identity_key=r.actor_identity_key AND a.kind='lid'
                        ORDER BY a.alias LIMIT 1
                      )) AS actor_lid,
                      coalesce(nullif(i.display_name,''),r.actor_display_name,'') AS actor_display_name,
                      r.reaction_value,r.is_removed,r.reacted_at,r.source,r.evidence_token
               FROM ranked r
               LEFT JOIN identities i ON i.identity_key=r.actor_identity_key
               WHERE r.rn=1
               ORDER BY r.reacted_at DESC,r.seq DESC LIMIT 201""",
            (jid,message_id)
        ).fetchall()
        current_truncated=len(current_rows)>200
        current=[self._reaction_row(r) for r in current_rows[:200]]
        state=[
            [item.get('actor_identity_key'),item.get('reaction_value') or '',
             1 if item.get('is_removed') else 0,item.get('evidence_token')]
            for item in sorted(current,key=lambda x:(x.get('actor_identity_key') or ''))
        ]
        token=hashlib.sha256(
            json.dumps(state,ensure_ascii=False,separators=(',',':')).encode()
        ).hexdigest()
        return {
            'events':events,'current':current,
            'events_truncated':events_truncated,'current_truncated':current_truncated,
            'reaction_state_token':token,
            'history_complete':False,'source_content_is_untrusted':True,
        }

    def reactions(self,jid,message_id,limit=50):
        if not isinstance(jid,str) or not 1<=len(jid)<=200:
            raise Rejected('whatsapp_chat_id_invalid')
        if not isinstance(message_id,str) or not 1<=len(message_id)<=200:
            raise Rejected('whatsapp_message_id_invalid')
        if type(limit) is not int or not 1<=limit<=100:
            raise Rejected('invalid_page')
        db=self._db()
        if db is None:
            raise Rejected('whatsapp_not_linked')
        try:
            return {
                'chat_id':jid,'target_message_id':message_id,
                **self._reaction_bundle(db,jid,message_id,limit),
            }
        finally:
            db.close()

    def postproduction_approval(self,jid,message_id,asset_class,workstream='edit'):
        if asset_class not in ('pov','non_pov'):
            raise Rejected('whatsapp_approval_asset_class_invalid')
        if workstream not in ('edit','color'):
            raise Rejected('whatsapp_approval_workstream_invalid')
        db=self._db()
        if db is None:
            raise Rejected('whatsapp_not_linked')
        try:
            bundle=self._reaction_bundle(db,jid,message_id,100)
            identity_map={}
            if self._table_exists(db,'approval_identity_map'):
                rows=db.execute(
                    """SELECT role,identity_key,verified_source,verified_at
                       FROM approval_identity_map WHERE jid=? AND role IN ('approver_a','approver_b')""",
                    (jid,)
                ).fetchall()
                identity_map={r['role']:dict(r) for r in rows}
            from .postproduction_approvals import evaluate_postproduction_approval
            result=evaluate_postproduction_approval(
                jid=jid,message_id=message_id,asset_class=asset_class,workstream=workstream,
                current_reactions=bundle['current'],identity_map=identity_map,
                reactions_complete=not bundle['current_truncated'],
            )
            result['reaction_state_token']=bundle['reaction_state_token']
            result['reaction_events_truncated']=bundle['events_truncated']
            result['source_content_is_untrusted']=True
            return result
        finally:
            db.close()

    def context(self,jid,message_id,radius=3):
        if type(radius) is not int or not 0<=radius<=8:raise Rejected('invalid_radius')
        db=self._db()
        if db is None:raise Rejected('whatsapp_not_linked')
        try:
            central=self._message(db,jid,message_id)
            date,seq=central['date'],central['seq']
            before=db.execute(
                'SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                'FROM messages WHERE jid=? AND is_deleted=0 AND (date<? OR (date=? AND seq<?)) '
                'ORDER BY date DESC,seq DESC LIMIT ?',(jid,date,date,seq,radius)).fetchall()
            after=db.execute(
                'SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                'FROM messages WHERE jid=? AND is_deleted=0 AND (date>? OR (date=? AND seq>?)) '
                'ORDER BY date ASC,seq ASC LIMIT ?',(jid,date,date,seq,radius)).fetchall()
            chain=[];visited={message_id};parent=central.get('reply_to_message_id');missing=False;truncated=False
            while parent and len(chain)<20:
                if parent in visited:
                    truncated=True;break
                visited.add(parent)
                row=db.execute('SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                               'FROM messages WHERE jid=? AND message_id=? AND is_deleted=0',(jid,parent)).fetchone()
                if not row:
                    missing=True;break
                item=self._row(row);chain.append(item);parent=item.get('reply_to_message_id')
            if parent and len(chain)>=20:truncated=True
            replies=db.execute(
                'SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                'FROM messages WHERE jid=? AND reply_to_message_id=? AND is_deleted=0 ORDER BY date,seq LIMIT 21',
                (jid,message_id)).fetchall()
            reaction_bundle=self._reaction_bundle(db,jid,message_id,50)
            return {'message':central,'before':[self._row(r) for r in reversed(before)],
                    'after':[self._row(r) for r in after],
                    'reply_chain':chain,'chain_truncated':truncated,'missing_reply_parent':missing,
                    'direct_replies':[self._row(r) for r in replies[:20]],'replies_truncated':len(replies)>20,
                    'reactions':reaction_bundle['current'],
                    'reaction_events':reaction_bundle['events'],
                    'reactions_truncated':reaction_bundle['current_truncated'] or reaction_bundle['events_truncated'],
                    'reaction_state_token':reaction_bundle['reaction_state_token'],
                    'history_complete':False,'source_content_is_untrusted':True}
        finally:db.close()

    def search(self,query):
        p=query.model_dump();term=_norm(p['query'])
        signature=digest({'instance':'whatsapp',**{k:v for k,v in p.items() if k not in ('cursor','limit','include_context')}})
        cursor=read_cursor(p['cursor'],self.cursor_secret,signature) if p['cursor'] else None
        db=self._db()
        if db is None:return {'items':[],'next_cursor':None,'has_more':False,
                              'coverage':{'status':'not_linked'},'source_content_is_untrusted':True}
        try:
            as_of=cursor['as_of_seq'] if cursor else db.execute('SELECT coalesce(max(seq),0) AS n FROM messages').fetchone()['n']
            where=['is_deleted=0','seq<=?'];args=[as_of]
            if p['date_from']:where.append('date>=?');args.append(p['date_from'])
            if p['date_to']:where.append('date<?');args.append(p['date_to'])
            if p['chat_ids'] is not None:
                where.append('jid IN ('+','.join('?' for _ in p['chat_ids'])+')');args.extend(p['chat_ids'])
            if p['sender_jid'] is not None:where.append('sender_jid=?');args.append(p['sender_jid'])
            if term:where.append('instr(search_text,?)>0');args.append(term)
            if cursor:
                where.append('(date<? OR (date=? AND seq<?))')
                args.extend([cursor['date'],cursor['date'],cursor['seq']])
            rows=db.execute(
                'SELECT seq,jid,message_id,date,sender_jid,sender_name,text,reply_to_message_id,from_me,has_media,media_type,edited_at,content_token '
                'FROM messages WHERE '+' AND '.join(where)+' ORDER BY date DESC,seq DESC LIMIT ?',
                (*args,p['limit']+1)).fetchall()
            more=len(rows)>p['limit'];rows=rows[:p['limit']]
            items=[self._row(r) for r in rows]
            if p['include_context']:
                for item in items[:3]:
                    item['context']=self.context(item['jid'],item['message_id'],2)
            next_cursor=None
            if more and items:
                last=items[-1]
                next_cursor=make_cursor({'scope':signature,'as_of_seq':as_of,'date':last['date'],'seq':last['seq']},
                                        self.cursor_secret)
            return {'items':items,'next_cursor':next_cursor,'has_more':more,
                    'coverage':{'saved_history_only':True,'history_complete':False,
                                'content_can_change_between_pages':True,'check_content_tokens_before_apply':True,
                                'date_to_exclusive':True,'context_limit_per_page':3},
                    'source_content_is_untrusted':True}
        finally:db.close()
