"""Instance-scoped configuration reads and immutable revisions."""
import re
from tm_api.v24.common import Rejected,check_revision,digest,encoded
from .configuration import render

def explicit(m):
    if not any(e.get('kind')=='user' and e.get('statement','').strip() and len(e.get('confirmation_ref',''))>=8 for e in m['evidence']):
        raise Rejected('configuration_requires_direct_user_instruction')

def actor(tx,instance):
    tx.execute("SELECT set_config('tm.v25_actor',%s,true)",(instance,))

def workspace(tx,instance,ident,lock=False,active=False):
    r=tx.one('SELECT * FROM tm_config.workspaces WHERE id=%s::uuid AND instance_id=%s'+(' FOR UPDATE' if lock else ''),(ident,instance))
    if not r:raise Rejected('workspace_not_found')
    if active and r['status']!='active':raise Rejected('workspace_not_active')
    return r

def by_group(tx,instance,group_id):
    return tx.one('SELECT * FROM tm_config.workspaces WHERE group_id=%s AND instance_id=%s',(group_id,instance))

def context(tx,instance,ident):
    w=workspace(tx,instance,ident,active=True)
    docs=tx.all("SELECT * FROM tm_config.documents WHERE instance_id=%s AND (workspace_id=%s::uuid OR workspace_id IS NULL) ORDER BY document_key,id",(instance,ident))
    defaults=tx.all('SELECT * FROM tm_config.defaults ORDER BY key')
    sources=tx.all('SELECT gm.chat_id,c.chat_name,gm.enabled,gm.managed_by_targets FROM public.telegram_chat_group_members gm JOIN public.telegram_chats c ON c.chat_id=gm.chat_id WHERE gm.group_id=%s ORDER BY gm.chat_id',(w['group_id'],))
    resolutions=tx.all('SELECT id,selector_kind,selector_value,enabled,resolved_chat_id,resolution_status,request_revision FROM public.telegram_chat_group_targets WHERE group_id=%s ORDER BY id',(w['group_id'],))
    token=digest({'workspace':w,'documents':docs,'defaults':defaults,'sources':sources,'resolutions':resolutions})
    active=[d for d in docs if d['status']=='active']
    rules=[d for d in active if d['kind']=='rule']
    rules.sort(key=lambda d:(d['workspace_id'] is None,-d['body'].get('priority',100),d['document_key']))
    conflicts=[];keys={}
    for r in rules:
        k=(r['workspace_id'],r['body'].get('decision_key'))
        if k[1] and k in keys and keys[k]['body'].get('value')!=r['body'].get('value'):
            conflicts.append([keys[k]['id'],r['id']])
        keys[k]=r
    return {'workspace':w,'configuration_token':token,'documents':docs,'active_rules':rules,'conflicts':conflicts,
      'defaults':{d['key']:d['body'] for d in defaults},'sources':sources,'source_resolutions':resolutions,
      'precedence':['current_explicit_user_instruction','workspace_rule','user_global_rule','server_defaults','review_if_uncertain'],
      'safety':'ACL, evidence, CAS, explicit approval and ledger integrity are not overridable by configuration.',
      'interpretation':'Calling assistant interprets natural-language rules. No semantic background model is installed here.',
      'learning':'A case answer is not a rule. Generalizations are proposals; activation is a separately confirmed mutation.'}

def history(tx,instance,kind,ident,revision,snapshot,evidence,operation):
    tx.execute('INSERT INTO tm_config.history(instance_id,resource_type,resource_id,revision,snapshot,evidence,operation) VALUES(%s,%s,%s::uuid,%s,%s::jsonb,%s::jsonb,%s)',
      (instance,kind,ident,revision,encoded(snapshot),encoded(evidence),operation))

def assert_context(ctx,m):
    if m.get('workspace_id') not in (None,ctx['workspace']['id']):raise Rejected('workspace_target_mismatch')
    if m.get('configuration_token')!=ctx['configuration_token']:raise Rejected('read_current_analysis_context_first')
    analysis=m.get('analysis') or {}
    allowed={'rule_refs','reason','current_user_instruction','entity_kind',
             'classification','finding_key','rule_key'}
    if not isinstance(analysis,dict) or set(analysis)-allowed:
        raise Rejected('invalid_analysis_provenance')
    classification=analysis.get('classification')
    finding_key=analysis.get('finding_key')
    rule_key=analysis.get('rule_key')
    if classification is not None and classification not in ('deterministic','ambiguous'):
        raise Rejected('invalid_analysis_classification')
    if finding_key is not None and (not isinstance(finding_key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{16,128}',finding_key)):
        raise Rejected('invalid_analysis_finding_key')
    if rule_key is not None and (not isinstance(rule_key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',rule_key)):
        raise Rejected('invalid_analysis_rule_key')
    binding=(classification is not None,finding_key is not None,rule_key is not None)
    if any(binding) and not all(binding):
        raise Rejected('incomplete_reconciliation_analysis_binding')
    refs=analysis.get('rule_refs',[])
    known={(r['id'],r['revision']) for r in ctx['active_rules']}
    if not isinstance(refs,list) or len(refs)>60 or any(not isinstance(r,dict) or set(r)!={'id','revision'} or (r['id'],r['revision']) not in known for r in refs):
        raise Rejected('stale_or_foreign_rule_reference')
    if len(encoded(analysis).encode())>18000:raise Rejected('analysis_provenance_too_large')

def effective_template(ctx,entity_kind,channel):
    candidates=[d for d in ctx['documents'] if d['status']=='active' and d['kind']=='template' and d['body']['entity_kind']==entity_kind and d['body']['channel']==channel]
    local=[d for d in candidates if d['workspace_id']==ctx['workspace']['id']]
    selected=local or [d for d in candidates if d['workspace_id'] is None]
    if len(selected)>1:raise Rejected('ambiguous_template_configuration')
    if selected:return selected[0]['body']['pattern']
    return ctx['defaults']['templates'].get(entity_kind,ctx['defaults']['templates']['task'])[channel]

def formatting(ctx):return ctx['defaults']['formatting']|ctx['workspace']['settings'].get('formatting',{})

def present(ctx,kind,values):
    return {channel:render(effective_template(ctx,kind,channel),values,500 if channel=='title' else 16000) for channel in ('title','description')}

def read_chats(tx,instance,ids):
    rows=[]
    for cid in sorted(ids):
        row=tx.one('SELECT chat_id,chat_name FROM public.telegram_chats c WHERE c.chat_id=%s AND c.enabled',(cid,))
        if not row:raise Rejected('chat_not_in_saved_catalog_resolve_first')
        foreign=tx.one('SELECT 1 AS found FROM public.telegram_chat_group_members gm JOIN public.telegram_chat_groups g ON g.id=gm.group_id WHERE gm.chat_id=%s AND gm.enabled AND g.reminder_list_instance_id IS NOT NULL AND g.reminder_list_instance_id<>%s LIMIT 1',(cid,instance))
        if foreign:raise Rejected('chat_belongs_to_another_instance')
        rows.append(row)
    return rows

def guard_token(tx,instance,m):
    ident=m.get('workspace_id')
    if not ident:return None
    ctx=context(tx,instance,ident);assert_context(ctx,m);return ctx
