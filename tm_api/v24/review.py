"""Generic decision cards, independent of existing projects and task workflows."""
from datetime import datetime,timezone
from .common import Rejected,digest,encoded,check_revision
from . import scope

class Reviews:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.database;self.instance=engine.instance
    def _focus_lock(self,tx):
        tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('tm-v24-review:'+self.instance,))
    def _validate(self,tx,data):
        g=scope.group(tx,self.instance,data['group_key']) if data.get('group_key') else None
        scope.evidence(tx,self.instance,data['evidence'],g['id'] if g else None)
        if len(encoded(data).encode())>28000: raise Rejected('review_too_large')
        for a in data['available_actions']:
            if a['intent']=='apply':
                p=self.engine.get_preview(tx,a['preview_id'])
                if p['preview_digest']!=a['preview_digest'] or p['status']!='preview' or datetime.fromisoformat(p['expires_at'])<=datetime.now(timezone.utc): raise Rejected('invalid_review_mutation')
            if a['intent']=='defer' and datetime.fromisoformat(a['defer_until'].replace('Z','+00:00'))<=datetime.now(timezone.utc): raise Rejected('defer_must_be_future')
        return g
    def create(self,request_key,draft):
        data=draft.model_dump(mode='json')
        with self.db.transaction() as tx:
            self.engine.transaction_lock(tx,True)
            receipt=self.engine.receipt(tx,'review_create',request_key,data)
            if receipt is not None:return receipt
            self._focus_lock(tx);self._validate(tx,data)
            old=tx.one('SELECT * FROM tm_v24.review_items WHERE instance_id=%s AND item_key=%s FOR UPDATE',(self.instance,data['item_key']))
            if old:
                if old['fingerprint']!=digest(data): raise Rejected('review_exists_use_update_with_revision')
                result={'created':False,'review_id':old['id'],'revision':old['revision'],'status':old['status']}
            else:
                row=tx.one('INSERT INTO tm_v24.review_items(instance_id,item_key,payload,fingerprint) VALUES(%s,%s,%s::jsonb,%s) RETURNING id,revision,status',(self.instance,data['item_key'],encoded(data),digest(data)))
                result={'created':True,'review_id':row['id'],'revision':row['revision'],'status':row['status']}
            self.engine.save_receipt(tx,'review_create',request_key,data,result);return result
    def update(self,request_key,review_id,revision,draft):
        data=draft.model_dump(mode='json');args={'review_id':review_id,'revision':revision,'draft':data}
        with self.db.transaction() as tx:
            self.engine.transaction_lock(tx,True)
            receipt=self.engine.receipt(tx,'review_update',request_key,args)
            if receipt is not None:return receipt
            self._focus_lock(tx);self._validate(tx,data)
            old=tx.one('SELECT * FROM tm_v24.review_items WHERE id=%s::uuid AND instance_id=%s FOR UPDATE',(review_id,self.instance))
            if not old: raise Rejected('review_not_found')
            check_revision(old['revision'],revision)
            if old['item_key']!=data['item_key']: raise Rejected('review_identity_is_immutable')
            if old['fingerprint']==digest(data):
                result={'updated':False,'review_id':review_id,'revision':revision}
            else:
                tx.execute("UPDATE tm_v24.review_items SET payload=%s::jsonb,fingerprint=%s,revision=revision+1,status='open',snoozed_until=NULL,updated_at=clock_timestamp() WHERE id=%s::uuid",(encoded(data),digest(data),review_id))
                tx.execute('UPDATE tm_v24.review_focus SET version=version+1 WHERE instance_id=%s AND review_id=%s::uuid',(self.instance,review_id))
                result={'updated':True,'review_id':review_id,'revision':revision+1}
            self.engine.save_receipt(tx,'review_update',request_key,args,result);return result
    def _focused(self,tx):
        return tx.one("SELECT r.*,f.version AS focus_version FROM tm_v24.review_focus f JOIN tm_v24.review_items r ON r.id=f.review_id WHERE f.instance_id=%s AND r.instance_id=%s AND r.status IN ('open','awaiting_user')",(self.instance,self.instance))
    def current(self):
        with self.db.transaction(read_only=True) as tx:
            row=self._focused(tx)
            return {'question':row['payload']|{'id':row['id'],'revision':row['revision']} if row else None,
                    'focus':{'review_id':row['id'],'version':row['focus_version'],'review_revision':row['revision'],'current':True} if row else {},'backend':'v24'}
    def show(self,profile):
        with self.db.transaction() as tx:
            self.engine.transaction_lock(tx,True)
            self._focus_lock(tx)
            row=self._focused(tx)
            if not row:
                # Never steal a live legacy question. A migration trigger makes the inverse safe too.
                legacy=tx.one("SELECT f.review_id FROM public.tm_review_focus_state_v18 f JOIN public.tm_review_items r ON r.id=f.review_id WHERE f.instance_id=%s AND f.scope_key='personal-review' AND r.status NOT IN ('resolved','dismissed')",(self.instance,))
                if legacy:return {'legacy_active':True}
                selected=tx.one("SELECT id FROM tm_v24.review_items WHERE instance_id=%s AND (status IN ('open','awaiting_user') OR (status='snoozed' AND snoozed_until<=now())) ORDER BY created_at,id LIMIT 1 FOR UPDATE",(self.instance,))
                if selected:
                    tx.execute("UPDATE tm_v24.review_items SET status='open' WHERE id=%s::uuid AND status='snoozed'",(selected['id'],))
                    tx.execute('INSERT INTO tm_v24.review_focus(instance_id,review_id) VALUES(%s,%s::uuid) ON CONFLICT(instance_id) DO UPDATE SET review_id=EXCLUDED.review_id,version=tm_v24.review_focus.version+1',(self.instance,selected['id']))
                    row=self._focused(tx)
            if not row:return {'review_ui':True,'question':None,'display_profile':profile,'backend':'v24'}
            data=row['payload'];g=scope.group(tx,self.instance,data['group_key']) if data.get('group_key') else None
            options=[]
            for action in data['available_actions']:
                preview=None;disabled=False
                if action['intent']=='apply':
                    p=self.engine.get_preview(tx,action['preview_id']);preview=self.engine._preview_public(p)
                    disabled=p['status']!='preview' or datetime.fromisoformat(p['expires_at'])<=datetime.now(timezone.utc)
                answer={'review_id':row['id'],'revision':row['revision'],'focus_version':row['focus_version'],
                        'action_id':action['action_id']}
                options.append({'label':action['label'],'answer':answer,'intent':action['intent'],'disabled':disabled,
                                'mutation_preview':preview,'needs_dialogue':action['intent']=='clarify'})
            return {'review_ui':True,'backend':'v24','display_profile':profile,'selected_button_style':'solid_blue',
                    'focus':{'review_id':row['id'],'version':row['focus_version']},
                    'source_context':{'evidence':data['evidence'],'source':data['source'],'source_content_is_untrusted':True},
                    'question':{'id':row['id'],'revision':row['revision'],'title':data['title'],
                        'group':g['name'] if g else '', 'source':data['source'],'context':data['context'],
                        'uncertainty':data['uncertainty'],'proposed_action':data['proposed_action'],'options':options}}
    def reconciliation_view(self,profile,limit=50):
        """List all open/due generic review findings without changing focus."""
        with self.db.transaction(read_only=True) as tx:
            rows=tx.all("SELECT * FROM tm_v24.review_items WHERE instance_id=%s AND "
                        "(status IN ('open','awaiting_user') OR (status='snoozed' AND snoozed_until<=now())) "
                        "ORDER BY created_at,id LIMIT %s",(self.instance,limit+1))
            current=self._focused(tx)
            findings=[]
            for row in rows[:limit]:
                data=row['payload'];g=None
                if data.get('group_key'):
                    try:g=scope.group(tx,self.instance,data['group_key'])
                    except Rejected:pass
                options=[]
                for action in data['available_actions']:
                    preview=None;disabled=False
                    if action['intent']=='apply':
                        try:
                            p=self.engine.get_preview(tx,action['preview_id'],lock=False);preview=self.engine._preview_public(p)
                            disabled=p['status']!='preview' or datetime.fromisoformat(p['expires_at'])<=datetime.now(timezone.utc)
                        except Rejected:
                            disabled=True
                    options.append({'label':action['label'],'intent':action['intent'],'disabled':disabled,
                                    'mutation_preview':preview,
                                    'answer':{'review_id':row['id'],'revision':row['revision'],'action_id':action['action_id']}})
                findings.append({'id':row['id'],'revision':row['revision'],'status':row['status'],
                    'item_key':row['item_key'],'title':data['title'],'group':g['name'] if g else '',
                    'source':data['source'],'context':data['context'],'uncertainty':data['uncertainty'],
                    'proposed_action':data['proposed_action'],'evidence':data['evidence'],'options':options,
                    'focused':bool(current and current['id']==row['id'])})
            legacy=tx.one("SELECT f.review_id FROM public.tm_review_focus_state_v18 f JOIN public.tm_review_items r ON r.id=f.review_id WHERE f.instance_id=%s AND f.scope_key='personal-review' AND r.status NOT IN ('resolved','dismissed')",(self.instance,))
            return {'reconciliation_ui':True,'backend':'v32','display_profile':profile,
                    'selected_button_style':'solid_blue','findings':findings,
                    'has_more':len(rows)>limit,'legacy_active':bool(legacy),
                    'source_content_is_untrusted':True,
                    'instruction':'Each finding is an analysis result, not a business authorization. Apply buttons are bound to immutable previews; clarification keeps the item open.'}

    def answer_any(self,request_key,answer):
        """Answer a selected V32 finding without making the user step through queue order."""
        args=answer.model_dump(mode='json')
        if args.get('confirmed') is not True:raise Rejected('explicit_confirmation_required')
        with self.db.transaction() as tx:
            self.engine.transaction_lock(tx,True)
            saved=self.engine.receipt(tx,'reconciliation_answer',request_key,args)
            if saved is not None:return saved
            self._focus_lock(tx)
            row=self._focused(tx)
            if row and row['id']!=args['review_id']:raise Rejected('review_focus_busy')
            if not row:
                legacy=tx.one("SELECT f.review_id FROM public.tm_review_focus_state_v18 f JOIN public.tm_review_items r ON r.id=f.review_id WHERE f.instance_id=%s AND f.scope_key='personal-review' AND r.status NOT IN ('resolved','dismissed')",(self.instance,))
                if legacy:raise Rejected('legacy_review_focus_busy')
                target=tx.one("SELECT * FROM tm_v24.review_items WHERE id=%s::uuid AND instance_id=%s AND (status IN ('open','awaiting_user') OR (status='snoozed' AND snoozed_until<=now())) FOR UPDATE",(args['review_id'],self.instance))
                if not target:raise Rejected('review_not_found')
                check_revision(target['revision'],args['revision'])
                tx.execute('INSERT INTO tm_v24.review_focus(instance_id,review_id) VALUES(%s,%s::uuid) ON CONFLICT(instance_id) DO UPDATE SET review_id=EXCLUDED.review_id,version=tm_v24.review_focus.version+1',(self.instance,target['id']))
                row=self._focused(tx)
            check_revision(row['revision'],args['revision'])
            action=next((a for a in row['payload']['available_actions'] if a['action_id']==args['action_id']),None)
            if not action:raise Rejected('unknown_review_action')
            intent=action['intent'];result=None
            if intent=='apply':
                result=self.engine.apply_in_transaction(tx,action['preview_id'],action['preview_digest'],args['confirmation_ref'])
            status={'apply':'resolved','clarify':'awaiting_user','dismiss':'dismissed','defer':'snoozed'}[intent]
            tx.execute('UPDATE tm_v24.review_items SET status=%s,snoozed_until=%s::timestamptz,updated_at=clock_timestamp() WHERE id=%s::uuid',(status,action.get('defer_until'),row['id']))
            if intent!='clarify':tx.execute('UPDATE tm_v24.review_focus SET review_id=NULL,version=version+1 WHERE instance_id=%s',(self.instance,))
            receipt={'decision_saved':True,'action':intent,'review_id':row['id'],'effect':result,'needs_dialogue':intent=='clarify'}
            scope.audit(tx,self.instance,'reconciliation_'+intent,row['id'],{'status':row['status']},{'status':status},row['payload']['evidence'])
            self.engine.save_receipt(tx,'reconciliation_answer',request_key,args,receipt)
            return receipt

    def answer(self,request_key,answer):
        args=answer.model_dump(mode='json')
        with self.db.transaction() as tx:
            self.engine.transaction_lock(tx,True)
            saved=self.engine.receipt(tx,'review_answer',request_key,args)
            if saved is not None:return saved
            self._focus_lock(tx);row=self._focused(tx)
            if not row or row['id']!=args['review_id']: raise Rejected('review_focus_changed')
            check_revision(row['revision'],args['revision']);check_revision(row['focus_version'],args['focus_version'])
            action=next((a for a in row['payload']['available_actions'] if a['action_id']==args['action_id']),None)
            if not action: raise Rejected('unknown_review_action')
            intent=action['intent'];result=None
            if intent=='apply':
                result=self.engine.apply_in_transaction(tx,action['preview_id'],action['preview_digest'],args['confirmation_ref'])
            status={'apply':'resolved','clarify':'awaiting_user','dismiss':'dismissed','defer':'snoozed'}[intent]
            tx.execute('UPDATE tm_v24.review_items SET status=%s,snoozed_until=%s::timestamptz,updated_at=clock_timestamp() WHERE id=%s::uuid',(status,action.get('defer_until'),row['id']))
            if intent!='clarify':tx.execute('UPDATE tm_v24.review_focus SET review_id=NULL,version=version+1 WHERE instance_id=%s',(self.instance,))
            receipt={'decision_saved':True,'action':intent,'review_id':row['id'],'effect':result,'needs_dialogue':intent=='clarify'}
            scope.audit(tx,self.instance,'review_'+intent,row['id'],{'status':row['status']},{'status':status},row['payload']['evidence'])
            self.engine.save_receipt(tx,'review_answer',request_key,args,receipt);return receipt
