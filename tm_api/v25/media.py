"""Guarded retry of saved Telegram media through the universal preview/apply layer."""
from copy import deepcopy
from tm_api.v24.common import Rejected, strict_fields
from tm_api.v24.scope import CHAT_SCOPE

OPS={'retry_attachment'}

class MediaBusiness:
    def __init__(self,instance):
        self.instance=instance

    def _row(self,tx,ident,lock=False):
        try:
            ident=int(ident)
        except (TypeError,ValueError):
            raise Rejected('invalid_attachment_id') from None
        suffix=' FOR UPDATE OF a' if lock else ''
        row=tx.one(
            'SELECT a.id,a.chat_id,a.message_id,a.file_name,a.kind,a.processing_status,a.processing_error,'
            'a.processing_details,a.source_available,a.source_token,'
            'public.tm_content_token_v11(m.chat_id,m.message_id) AS content_token '
            'FROM public.telegram_attachments a JOIN public.telegram_messages m '
            'ON m.chat_id=a.chat_id AND m.message_id=a.message_id '
            'WHERE a.id=%s AND a.source_token=m.media_source_token AND NOT m.is_deleted '
            'AND a.deleted_at IS NULL AND '+CHAT_SCOPE+suffix,
            (ident,self.instance))
        if not row:
            raise Rejected('attachment_not_found')
        return row

    def plan(self,tx,m,lock=False):
        if m['operation']!='retry_attachment':
            raise Rejected('unsupported_media_operation')
        strict_fields(m['changes'],('reason',),('reason',))
        reason=m['changes']['reason']
        if not isinstance(reason,str) or not 3<=len(reason)<=300:
            raise Rejected('invalid_retry_reason')
        row=self._row(tx,m['target_id'],lock)
        if str(m['expected_revision'])!=row['content_token']:
            raise Rejected('attachment_content_changed')
        if not row['source_available']:
            raise Rejected('attachment_source_unavailable')
        if row['processing_status'] not in ('skipped','failed','partial','needs_setup','source_unavailable'):
            raise Rejected('attachment_retry_not_needed')
        before={k:row.get(k) for k in ('id','chat_id','message_id','file_name','kind','processing_status','processing_error','source_available','content_token')}
        after=deepcopy(before)
        after['processing_status']='retry_requested'
        after['processing_error']=None
        return {'entity':'telegram_attachment','operation':'retry_attachment','target_id':row['id'],
                'before':before,'after':after,'chat_id':row['chat_id'],'message_id':row['message_id']}

    def execute(self,tx,m,plan):
        result=tx.one('SELECT public.tm_retry_attachment_v10(%s,%s) AS result',
                      (plan['chat_id'],plan['message_id']))['result']
        row=self._row(tx,plan['target_id'])
        return {'entity':'telegram_attachment','id':row['id'],'chat_id':row['chat_id'],
                'message_id':row['message_id'],'retry_result':result,
                'processing_status':row['processing_status'],'source_available':row['source_available']}
