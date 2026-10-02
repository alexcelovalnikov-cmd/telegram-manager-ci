"""Guarded WhatsApp history backfill request through preview/apply."""
from uuid import NAMESPACE_URL, uuid5

from .v24.common import Rejected, strict_fields

OPS={'create_whatsapp_history_backfill_request'}


def stable(instance,key):
    return str(uuid5(NAMESPACE_URL,'tm-whatsapp-backfill:'+instance+':'+key))


class WhatsAppBackfillBusiness:
    def __init__(self,instance,reader):
        self.instance=instance
        self.reader=reader

    def plan(self,tx,m,lock=False):
        if m['operation'] not in OPS:
            raise Rejected('unsupported_whatsapp_backfill_operation')
        strict_fields(m['changes'],('chat_id','count'),('chat_id','count'))
        chat_id=m['changes']['chat_id']
        count=m['changes']['count']
        info=self.reader.plan_history_backfill(chat_id,count)
        ident=stable(self.instance,m['dedupe_key'])
        after={
            'id':ident,
            'chat_id':info['jid'],
            'chat_name':info['chat_name'],
            'count':info['count'],
            'anchor_message_id':info['anchor_message_id'],
            'anchor_date':info['anchor_date'],
            'status':'approved_for_queue',
        }
        return {
            'entity':'whatsapp_history_backfill_request',
            'operation':m['operation'],
            'target_id':ident,
            'before':None,
            'after':after,
            '_delivery':{
                'request_id':ident,
                'jid':info['jid'],
                'count':info['count'],
                'anchor_message_id':info['anchor_message_id'],
                'anchor_date':info['anchor_date'],
            },
        }

    def execute(self,tx,m,plan):
        return {
            'entity':'whatsapp_history_backfill_request',
            'id':plan['target_id'],
            'status':'approved_for_queue',
        }

    def post_commit(self,m,plan):
        delivery=plan.get('_delivery')
        if not delivery:
            raise Rejected('whatsapp_backfill_delivery_missing')
        return self.reader.enqueue_history_backfill(**delivery)
