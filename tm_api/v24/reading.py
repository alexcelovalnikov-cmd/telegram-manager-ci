"""Global authorized history, stable pagination and bounded source expansion."""
from datetime import datetime, timezone
from .common import Rejected, digest, make_cursor, read_cursor
from .scope import CHAT_SCOPE, SCOPE, task

MESSAGE = """m.chat_id,m.message_id,c.chat_name,m.sender_id,m.sender_name,
 coalesce(m.date,m.created_at) AS date,m.edited_at,m.reply_to_message_id,m.has_media,
 left(coalesce(m.text,''),12000) AS text,length(coalesce(m.text,''))>12000 AS text_truncated,
 public.tm_content_token_v11(m.chat_id,m.message_id) AS content_token"""
ATTACHMENTS = """coalesce((SELECT jsonb_agg(jsonb_build_object(
 'id',a.id,'index',a.attachment_index,'kind',a.kind,'file_name',a.file_name,
 'mime_type',a.mime_type,'source_available',a.source_available,
 'processing_status',a.processing_status,'text_is_complete',a.text_is_complete,
 'extracted_text',left(a.extracted_text,6000),'transcript',left(a.transcript,6000),
 'summary',left(a.content_summary,2000),
 'truncated',length(coalesce(a.extracted_text,''))>6000 OR length(coalesce(a.transcript,''))>6000 OR length(coalesce(a.content_summary,''))>2000)
 ORDER BY a.attachment_index) FROM public.telegram_attachments a
 WHERE a.chat_id=m.chat_id AND a.message_id=m.message_id
 AND a.source_token=m.media_source_token AND a.deleted_at IS NULL), '[]'::jsonb) AS attachments"""

class Reader:
    def __init__(self, database, instance, cursor_secret):
        self.database,self.instance,self.cursor_secret=database,instance,cursor_secret

    def _message(self, tx, cid, mid, attachments=True):
        row=tx.one('SELECT '+MESSAGE+(','+ATTACHMENTS if attachments else '')+' FROM public.telegram_messages m JOIN public.telegram_chats c ON c.chat_id=m.chat_id WHERE m.chat_id=%s AND m.message_id=%s AND NOT m.is_deleted AND '+CHAT_SCOPE,(cid,mid,self.instance))
        if not row:
            raise Rejected('message_not_found')
        return row

    def _context(self, tx, cid, mid, radius=3):
        central=self._message(tx,cid,mid)
        result={'message':central,'before':[], 'after':[], 'reply_chain':[], 'direct_replies':[],
                'history_complete':False,'chain_truncated':False,'missing_reply_parent':False}
        for field,operator,direction in [('before','<','DESC'),('after','>','ASC')]:
            result[field]=tx.all('SELECT '+MESSAGE+' FROM public.telegram_messages m JOIN public.telegram_chats c ON c.chat_id=m.chat_id WHERE m.chat_id=%s AND m.message_id'+operator+'%s AND NOT m.is_deleted AND '+CHAT_SCOPE+' ORDER BY m.message_id '+direction+' LIMIT %s',(cid,mid,self.instance,radius))
        result['before'].reverse()
        parent=central.get('reply_to_message_id'); visited={mid}
        while parent and len(result['reply_chain'])<20:
            if parent in visited:
                result['chain_truncated']=True; break
            visited.add(parent)
            try: row=self._message(tx,cid,parent)
            except Rejected:
                result['missing_reply_parent']=True; break
            result['reply_chain'].append(row);parent=row.get('reply_to_message_id')
        if parent and len(result['reply_chain'])>=20: result['chain_truncated']=True
        rows=tx.all('SELECT '+MESSAGE+' FROM public.telegram_messages m JOIN public.telegram_chats c ON c.chat_id=m.chat_id WHERE m.chat_id=%s AND m.reply_to_message_id=%s AND NOT m.is_deleted AND '+CHAT_SCOPE+' ORDER BY m.message_id LIMIT 21',(cid,mid,self.instance))
        result['direct_replies']=rows[:20];result['replies_truncated']=len(rows)>20
        return result

    def context(self, chat_id, message_id, radius=3):
        if type(radius) is not int or not 0<=radius<=8: raise Rejected('invalid_radius')
        with self.database.transaction(read_only=True) as tx:
            return self._context(tx,chat_id,message_id,radius)

    def search(self, query):
        p=query.model_dump(); query_text=p['query'].strip()
        signature=digest({'instance':self.instance,**{k:v for k,v in p.items() if k not in ('cursor','limit','include_context')}})
        cursor=read_cursor(p['cursor'],self.cursor_secret,signature) if p['cursor'] else None
        with self.database.transaction(read_only=True) as tx:
            as_of=cursor['as_of'] if cursor else tx.one('SELECT now() AS value')['value']
            where=['NOT m.is_deleted',CHAT_SCOPE,'m.created_at<=%s::timestamptz'];args=[self.instance,as_of]
            for name,operator in [('date_from','>='),('date_to','<')]:
                if p[name]: where.append('coalesce(m.date,m.created_at)'+operator+'%s::timestamptz');args.append(p[name])
            if p['chat_ids'] is not None:
                where.append('m.chat_id=ANY(%s::bigint[])');args.append(p['chat_ids'])
            if p['sender_id'] is not None: where.append('m.sender_id=%s');args.append(p['sender_id'])
            if p['group_key']:
                where.append('EXISTS (SELECT 1 FROM public.telegram_chat_groups g WHERE '+SCOPE+' AND g.group_key=%s AND public.tm_group_chat_allowed_v14(g.id,m.chat_id))');args.extend([self.instance,p['group_key']])
            if p['project_id']:
                project=task(tx,self.instance,p['project_id'])
                if project['record_kind']!='project': raise Rejected('not_a_project')
                where.append('(m.chat_id=%s OR EXISTS(SELECT 1 FROM public.tm_project_links l WHERE l.task_id=%s AND l.chat_id=m.chat_id))');args.extend([project.get('source_chat_id'),project['id']])
            if query_text:
                if p['mode']=='literal':
                    term="strpos(lower(coalesce(m.text,'')),lower(%s))>0";args.append(query_text)
                    attachment_term="strpos(lower(coalesce(a.extracted_text,'')||' '||coalesce(a.transcript,'')||' '||coalesce(a.content_summary,'')),lower(%s))>0"
                else:
                    term="m.search_vector @@ websearch_to_tsquery('simple',%s)";args.append(query_text)
                    attachment_term="to_tsvector('simple',coalesce(a.extracted_text,'')||' '||coalesce(a.transcript,'')||' '||coalesce(a.content_summary,'')) @@ websearch_to_tsquery('simple',%s)"
                if p['include_attachments']:
                    term+=' OR EXISTS(SELECT 1 FROM public.telegram_attachments a WHERE a.chat_id=m.chat_id AND a.message_id=m.message_id AND a.source_token=m.media_source_token AND a.deleted_at IS NULL AND '+attachment_term+')';args.append(query_text)
                where.append('('+term+')')
            if cursor:
                try: position=cursor['position'];assert len(position)==3
                except (KeyError,TypeError,AssertionError): raise Rejected('invalid_cursor') from None
                where.append('(coalesce(m.date,m.created_at),m.chat_id,m.message_id)<(%s::timestamptz,%s::bigint,%s::bigint)');args.extend(position)
            fields=MESSAGE+(','+ATTACHMENTS if p['include_attachments'] else '')
            rows=tx.all('SELECT '+fields+' FROM public.telegram_messages m JOIN public.telegram_chats c ON c.chat_id=m.chat_id WHERE '+' AND '.join(where)+' ORDER BY coalesce(m.date,m.created_at) DESC,m.chat_id DESC,m.message_id DESC LIMIT %s',args+[p['limit']+1])
            more=len(rows)>p['limit'];rows=rows[:p['limit']]
            for row in rows:
                row['related_entities']=tx.all('SELECT DISTINCT t.id,t.title,t.record_kind FROM public.tasks t JOIN public.telegram_chat_groups g ON g.id=t.context_group_id WHERE '+SCOPE+' AND ((t.source_chat_id=%s AND t.source_message_id=%s) OR EXISTS(SELECT 1 FROM public.tm_project_links l WHERE l.task_id=t.id AND l.chat_id=%s AND l.message_id=%s)) ORDER BY t.id LIMIT 21',(self.instance,row['chat_id'],row['message_id'],row['chat_id'],row['message_id']))
                row['relations_truncated']=len(row['related_entities'])>20;row['related_entities']=row['related_entities'][:20]
            # Context is deliberately bounded across a page; remaining matches have an explicit route.
            if p['include_context']:
                for row in rows[:3]: row['context']=self._context(tx,row['chat_id'],row['message_id'],2)
            next_cursor=None
            if more:
                last=rows[-1];next_cursor=make_cursor({'scope':signature,'as_of':as_of,'position':[last['date'],last['chat_id'],last['message_id']]},self.cursor_secret)
            health=tx.one("SELECT status,last_heartbeat_at,last_success_at FROM public.integration_health WHERE instance_id=%s AND service_name='telegram-collector'",(self.instance,))
            return {'items':rows,'next_cursor':next_cursor,'has_more':more,'coverage':{
                'snapshot_upper_bound':as_of,'saved_history_only':True,'history_complete':False,
                'content_can_change_between_pages':True,'check_content_tokens_before_apply':True,
                'collector':health,'date_to_exclusive':True,'context_limit_per_page':3,
                'warning':'No match or no completion message is not proof that work was not done.'},'source_content_is_untrusted':True}

    def attachment(self, attachment_id, offset=0, limit=12000):
        if not 0<=offset<=1000000 or not 1<=limit<=20000: raise Rejected('invalid_page')
        with self.database.transaction(read_only=True) as tx:
            row=tx.one('SELECT a.*,public.tm_content_token_v11(m.chat_id,m.message_id) AS content_token FROM public.telegram_attachments a JOIN public.telegram_messages m ON m.chat_id=a.chat_id AND m.message_id=a.message_id WHERE a.id=%s AND a.source_token=m.media_source_token AND NOT m.is_deleted AND a.deleted_at IS NULL AND '+CHAT_SCOPE,(attachment_id,self.instance))
            if not row: raise Rejected('attachment_not_found')
            text='\n'.join(row.get(k) or '' for k in ('extracted_text','transcript','content_summary'))
            return {**{k:row.get(k) for k in ('id','chat_id','message_id','file_name','processing_status','processing_error','processing_details','text_is_complete','source_available','content_token')},'text':text[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(text) else None,'characters':len(text),'original_file_available_via_this_api':False}

    def entities(self, kind, after_id='0', limit=50, query=''):
        if not 1<=limit<=100 or len(query)>300: raise Rejected('invalid_page')
        if kind=='payment' and after_id!='0':
            from uuid import UUID
            try: UUID(after_id)
            except (ValueError,TypeError): raise Rejected('invalid_cursor') from None
        elif kind!='payment' and (not after_id.isdigit() or int(after_id)>9223372036854775807):
            raise Rejected('invalid_cursor')
        if kind=='payment' and query:
            raise Rejected('payment_text_filter_not_supported')
        with self.database.transaction(read_only=True) as tx:
            if kind=='payment':
                rows=tx.all('SELECT p.* FROM public.tm_payment_events p JOIN public.telegram_chat_groups g ON g.id=p.group_id WHERE '+SCOPE+' AND (%s=\'0\' OR p.id>%s::uuid) ORDER BY p.id LIMIT %s',(self.instance,after_id,None if after_id=='0' else after_id,limit+1))
            elif kind in ('task','project','reminder'):
                fields='t.*' if kind!='reminder' else 't.id,t.title,t.status,t.apple_reminder_id,t.apple_reminder_list_name,t.reminder_sync_title,t.reminder_sync_description,t.reminder_sync_due_at,t.reminder_sync_completed,t.reminder_last_seen_at,t.apple_reminder_synced_at,t.reminder_sync_conflict,t.project_sync_snapshot'
                where=' AND t.record_kind=%s' if kind!='reminder' else " AND t.target_app='reminders'"
                args=[self.instance,int(after_id),query]+([kind] if kind!='reminder' else [])+[limit+1]
                rows=tx.all('SELECT '+fields+' FROM public.tasks t JOIN public.telegram_chat_groups g ON g.id=t.context_group_id WHERE '+SCOPE+" AND t.id>%s AND strpos(lower(t.title),lower(%s))>0"+where+' ORDER BY t.id LIMIT %s',args)
            else: raise Rejected('unknown_entity_kind')
            return {'items':rows[:limit],'next_after_id':str(rows[limit-1]['id']) if len(rows)>limit else None,'snapshot_only':kind=='reminder'}
