"""Bounded configuration/catalog/provenance reads; never return credentials."""
import re
from tm_api.v24.common import Rejected
from tm_api.v24 import scope
from tm_calendar import repository as calendar
from . import repository as repo,finance

def _source_name(value):
    return re.sub(r'\s+',' ',str(value or '').strip().casefold().replace('ё','е'))


def _project_identity(value):
    return re.sub(r'[^a-zа-я0-9]+',' ',str(value or '').casefold().replace('ё','е')).strip()


def _strong_project_identity_match(wanted, value):
    candidate=_project_identity(value)
    if not wanted or not candidate:
        return False
    if wanted==candidate:
        return True
    shorter,longer=(wanted,candidate) if len(wanted)<=len(candidate) else (candidate,wanted)
    # A chat-name variant may add a role/date suffix to an existing project title.
    # Require at least three normalized tokens so generic client names cannot bind.
    return len(shorter)>=12 and len(shorter.split())>=3 and (' '+shorter+' ') in (' '+longer+' ')


def umbrella_binding_hint(rows, project_identity):
    """Resolve one strong existing umbrella identity; ambiguity always fails closed."""
    wanted=_project_identity(project_identity)
    if not wanted:
        return {'status':'ambiguous','entity_id':None}
    matches=[]
    for row in rows:
        data=(row or {}).get('data') or {}
        strong_keys=[data.get('title'),*(data.get('source_chats') or [])]
        exact_keys=[data.get('client')]
        if (any(_strong_project_identity_match(wanted,value) for value in strong_keys if value)
                or wanted in {_project_identity(value) for value in exact_keys if value}):
            matches.append(str(row.get('id')))
    matches=list(dict.fromkeys(matches))
    if len(matches)==1:
        return {'status':'bind_existing','entity_id':matches[0]}
    if len(matches)>1:
        return {'status':'ambiguous','entity_id':None}
    return {'status':'no_strong_match','entity_id':None}


class ConfigReader:
    def __init__(self,database,instance):self.database=database;self.instance=instance
    def _source_exclusions(self,tx):
        rows=tx.all("SELECT body FROM tm_config.documents WHERE instance_id=%s AND workspace_id IS NULL AND kind='rule' AND status='active'",(self.instance,))
        names=set()
        chat_ids=set()
        for row in rows:
            body=(row or {}).get('body') or {}
            value=body.get('value')
            if not isinstance(value,list):
                continue
            if body.get('decision_key')=='source_selection.excluded_names':
                for name in value:
                    if isinstance(name,str) and name.strip():
                        names.add(_source_name(name))
            elif body.get('decision_key')=='source_selection.excluded_chat_ids':
                for raw in value:
                    if isinstance(raw,bool):
                        continue
                    if isinstance(raw,int):
                        ident=raw
                    elif isinstance(raw,str) and raw.strip().lstrip('-').isdigit():
                        ident=int(raw.strip())
                    else:
                        continue
                    if -9223372036854775808 <= ident <= 9223372036854775807:
                        chat_ids.add(ident)
        return names,chat_ids
    def _postproduction_e2e(self,tx,ctx):
        sources=[row for row in ctx['sources'] if row.get('enabled')]
        history=[]
        for source in sources:
            row=tx.one(
                "SELECT count(*)::int AS saved_messages,max(date) AS latest_message_at "
                "FROM public.telegram_messages WHERE chat_id=%s AND NOT is_deleted",
                (source['chat_id'],)) or {}
            history.append({
                'chat_id':source['chat_id'],'chat_name':source.get('chat_name'),
                'saved_messages':int(row.get('saved_messages') or 0),
                'latest_message_at':row.get('latest_message_at'),
                'managed_by_targets':bool(source.get('managed_by_targets')),
            })
        health=tx.one(
            "SELECT status,last_heartbeat_at,last_success_at,details FROM public.integration_health "
            "WHERE instance_id=%s AND service_name='telegram-collector'",(self.instance,)) or {}
        details=health.get('details') or {}
        candidate_by_id={int(item['chat_id']):item for item in details.get('dialog_candidates') or []
                         if isinstance(item,dict) and str(item.get('chat_id','')).lstrip('-').isdigit()}
        forum_sources=[]
        for item in history:
            candidate=candidate_by_id.get(int(item['chat_id']))
            if candidate and candidate.get('forum'):
                forum_sources.append({'chat_id':item['chat_id'],'chat_name':item['chat_name'],
                                      'topic_titles':list(candidate.get('topic_titles') or [])[:30]})
        managed=tx.one(
            "SELECT count(*)::int AS total,"
            "count(*) FILTER (WHERE entity_type='postproduction-job')::int AS jobs,"
            "count(*) FILTER (WHERE entity_type='postproduction-item')::int AS items "
            "FROM tm_config.entities WHERE instance_id=%s AND workspace_id=%s::uuid AND status='active'",
            (self.instance,ctx['workspace']['id'])) or {}
        umbrellas=tx.all(
            "SELECT id,data FROM tm_config.entities WHERE instance_id=%s AND workspace_id=%s::uuid "
            "AND entity_type='postproduction-job' AND status='active' ORDER BY updated_at DESC,id LIMIT 100",
            (self.instance,ctx['workspace']['id']))
        analysis=tx.one(
            "SELECT count(*)::int AS count,max(created_at) AS latest_at "
            "FROM tm_config.analysis_receipts WHERE instance_id=%s AND workspace_id=%s::uuid",
            (self.instance,ctx['workspace']['id'])) or {}
        destination=(ctx['workspace'].get('settings') or {}).get('reminder_list') or {}
        reminder_count=None
        if destination.get('mode') in ('server_new','server_existing') and destination.get('id'):
            collection=calendar.access(tx,calendar.api_user(tx,self.instance),destination['id'])
            if collection.get('component_type')=='VTODO':
                row=tx.one(
                    "SELECT count(*)::int AS count FROM tm_calendar.events "
                    "WHERE calendar_id=%s::uuid AND deleted_at IS NULL",(destination['id'],)) or {}
                reminder_count=int(row.get('count') or 0)

        collector_running=health.get('status')=='running'
        with_history=sum(1 for item in history if item['saved_messages']>0)
        discovery=details.get('postproduction_discovery') or {}
        exclusion_guard=details.get('source_exclusion_guard') or {}
        safe_skips=[{'status':'skipped_by_exclusion'}
                    for item in (discovery.get('skipped') or [])
                    if isinstance(item,dict) and item.get('status')=='skipped_by_exclusion']
        candidate_bundle=[item for item in (discovery.get('candidates') or [])
                          if isinstance(item,dict)][:100]
        classification_counts={}
        for item in candidate_bundle:
            state=item.get('classification') or 'unknown'
            classification_counts[state]=classification_counts.get(state,0)+1
        umbrella_index=[]
        for row in umbrellas:
            data=row.get('data') or {}
            umbrella_index.append({
                'id':row['id'],
                'title':data.get('title'),
                'client':data.get('client'),
                'source_chats':list(data.get('source_chats') or [])[:30],
            })
        project_binding_hints=[]
        for item in candidate_bundle:
            if not item.get('activation_eligible'):
                continue
            project_binding_hints.append({
                'chat_id':item.get('chat_id'),
                'chat_name':item.get('name'),
                'binding':umbrella_binding_hint(umbrellas,item.get('name')),
            })

        reasons=[]
        if reminder_count==0:
            reasons.extend(['no_background_semantic_executor',
                            'managed_entities_do_not_auto_project_to_reminders'])
            if with_history<len(history):reasons.append('one_or_more_sources_have_no_saved_history')
            if not collector_running:reasons.append('telegram_collector_not_running')
        if not exclusion_guard.get('available'):
            reasons.append('source_exclusion_guard_unavailable_discovery_fails_closed')

        return {
            'read_only':True,
            'dialog_candidate_stage':{
                'scan_limit':200,'published_limit':100,
                'scanned_at':details.get('dialog_candidates_scanned_at'),
                'published':len(details.get('dialog_candidates') or []),
            },
            'source_exclusion_stage':{
                'available':bool(exclusion_guard.get('available')),
                'applied_before_content':bool(exclusion_guard.get('applied_before_content')),
                'excluded_name_count':int(exclusion_guard.get('excluded_name_count') or 0),
                'excluded_chat_id_count':int(exclusion_guard.get('excluded_chat_id_count') or 0),
                'skipped':safe_skips,
                'excluded_content_exposed':False,
            },
            'candidate_evidence_stage':{
                'private_current_message_limit':discovery.get('private_evidence_limit'),
                'candidate_media_processed':False,
                'private_previews_exposed_on_dialog_catalog':False,
                'bounded_candidates':candidate_bundle,
            },
            'post_start_classification_stage':{
                'classifier':discovery.get('classifier'),
                'deterministic':True,'explainable':True,'fail_closed_on_ambiguity':True,
                'counts':classification_counts,
                'allowed_start_signals':['edit_or_draft','color','cleanup_retouch_vfx_cg','sound',
                                         'client_corrections','export_delivery','approval','waiting_feedback_after_post'],
                'non_start_alone':['shoot','estimate','payment','equipment','logistics',
                                   'participant_intro','future_post_discussion'],
            },
            'workspace_source_activation_stage':{
                'deterministic_post_started_only':True,
                'private_allowed_when_bounded_and_non_excluded':True,
                'rechecks_exclusion_before_activation':True,
                'last':details.get('dynamic_source_activation_last'),
            },
            'source_stage':{'configured':len(history),'with_saved_history':with_history,
                            'history_complete':False,'items':history},
            'topic_routing_stage':{'forum_sources_seen_in_bounded_dialog_metadata':forum_sources,
                                   'collector_backfill_pending':details.get('dynamic_backfill_pending'),
                                   'collector_backfill_last':details.get('dynamic_backfill_last'),
                                   'backfill_rechecks_exclusion_before_history':True},
            'semantic_stage':{'interpreter':'calling_assistant','background_semantic_executor':False,
                              'deterministic_start_classifier_only':True,
                              'ambiguous_source_requires_assistant_or_user':True,
                              'analysis_receipts':int(analysis.get('count') or 0),
                              'latest_analysis_at':analysis.get('latest_at')},
            'managed_stage':{'active_entities':int(managed.get('total') or 0),
                             'umbrella_jobs':int(managed.get('jobs') or 0),
                             'work_items':int(managed.get('items') or 0),
                             'reminder_projection_supported':True,
                             'reminder_projection_operation':'update_postproduction_reminder_projection'},
            'project_binding_stage':{
                'one_umbrella_per_real_project':True,
                'compare_existing_before_create':True,
                'unique_strong_identity_binds_existing':True,
                'chat_change_never_creates_umbrella_automatically':True,
                'ambiguous_identity_requires_short_user_question':True,
                'existing_umbrellas':umbrella_index,
                'candidate_hints':project_binding_hints,
            },
            'concrete_user_action_stage':{
                'waiting_feedback_creates_reminder':False,
                'requires_concrete_action_for_owner':True,
                'title_is_next_action':True,
                'description_is_short_issue':True,
                'ambiguous_assignment_must_not_create_reminder':True,
            },
            'reminder_stage':{'list_id':destination.get('id'),'mode':destination.get('mode'),
                              'items':reminder_count},
            'collector':{'status':health.get('status'),'last_heartbeat_at':health.get('last_heartbeat_at'),
                         'last_success_at':health.get('last_success_at')},
            'controlled_pass_ready':bool(collector_running and exclusion_guard.get('available') and with_history),
            'empty_list_explanation':reasons,
        }

    def read(self,kind,workspace_id=None,resource_id=None,after_id=None,limit=50,status=None,entity_type=None,parent_id=None):
        if not 1<=limit<=100:raise Rejected('invalid_limit')
        if kind=='source_exclusions':
            with self.database.transaction(read_only=True) as tx:
                names,chat_ids=self._source_exclusions(tx)
            return {'excluded_names':sorted(names),'excluded_chat_ids':sorted(chat_ids),
                    'privacy_guard':'apply_before_candidate_content'}
        if kind=='schema':
            from .configuration import Workspace,Document,EntityType,Rule,Template
            from tm_reminders.models import Reminder
            return {'version':'V25','workspace':Workspace.model_json_schema(),'document':Document.model_json_schema(),'entity_type':EntityType.model_json_schema(),'reminder':Reminder.model_json_schema(),'semantics':{'workspace_is_not_billing_project':True,'natural_language_interpreter':'calling_assistant','writes':'preview_then_explicit_apply','generalization':'proposed_then_separate_activation','custom_entity_reminder_projection_supported':False,'managed_entity_hierarchy_supported':True,'managed_entity_sections_supported':False,'source_selection_exclusions_supported':True,'source_selection_exclusion_keys':['source_selection.excluded_names','source_selection.excluded_chat_ids'],'legacy_list_migration_supported':True,'workspace_storage_migration_operation':'migrate_workspace_storage','money':'decimal_strings_net_work_only; optional_explicit_tax_and_document_total'}}
        with self.database.transaction(read_only=True) as tx:
            calendar.lock(tx,write=False)
            if kind=='analysis_context':
                if not workspace_id:raise Rejected('workspace_id_required')
                ctx=repo.context(tx,self.instance,workspace_id)
                if ((ctx['workspace'].get('settings') or {}).get('analysis') or {}).get('profile')=='postproduction':
                    ctx['e2e_diagnostic']=self._postproduction_e2e(tx,ctx)
                return ctx
            if kind=='workspaces':
                rows=tx.all('SELECT * FROM tm_config.workspaces WHERE instance_id=%s AND (%s::uuid IS NULL OR id>%s::uuid) AND (%s::text IS NULL OR status=%s) ORDER BY id LIMIT %s',
                   (self.instance,after_id,after_id,status,status,limit+1))
            elif kind=='workspace':return {'workspace':repo.workspace(tx,self.instance,workspace_id)}
            elif kind=='documents':
                if workspace_id:repo.workspace(tx,self.instance,workspace_id)
                rows=tx.all('SELECT * FROM tm_config.documents WHERE instance_id=%s AND workspace_id IS NOT DISTINCT FROM %s::uuid AND (%s::uuid IS NULL OR id>%s::uuid) AND (%s::text IS NULL OR status=%s) ORDER BY id LIMIT %s',
                    (self.instance,workspace_id,after_id,after_id,status,status,limit+1))
            elif kind=='history':
                if not resource_id:raise Rejected('resource_id_required')
                cursor=int(after_id or '0')
                rows=tx.all('SELECT * FROM tm_config.history WHERE instance_id=%s AND resource_id=%s::uuid AND id>%s ORDER BY id LIMIT %s',(self.instance,resource_id,cursor,limit+1))
            elif kind=='entities':
                repo.workspace(tx,self.instance,workspace_id)
                where=['instance_id=%s','workspace_id=%s::uuid','(%s::uuid IS NULL OR id>%s::uuid)']
                args=[self.instance,workspace_id,after_id,after_id]
                if status:
                    if status not in ('active','archived'):raise Rejected('invalid_entity_status')
                    where.append('status=%s');args.append(status)
                if entity_type:
                    where.append('entity_type=%s');args.append(entity_type)
                if parent_id:
                    where.append("data->>'parent_id'=%s");args.append(parent_id)
                args.append(limit+1)
                rows=tx.all('SELECT * FROM tm_config.entities WHERE '+' AND '.join(where)+' ORDER BY id LIMIT %s',tuple(args))
            elif kind=='analysis_history':
                if not workspace_id:raise Rejected('workspace_id_required')
                repo.workspace(tx,self.instance,workspace_id)
                rows=tx.all('SELECT * FROM tm_config.analysis_receipts WHERE instance_id=%s AND workspace_id=%s::uuid AND id>%s ORDER BY id LIMIT %s',(self.instance,workspace_id,int(after_id or '0'),limit+1))
            elif kind=='chat_catalog':
                excluded_names,excluded_ids=self._source_exclusions(tx)
                rows=tx.all('SELECT c.chat_id AS id,c.chat_name FROM public.telegram_chats c WHERE c.enabled AND c.chat_id>%s AND NOT EXISTS(SELECT 1 FROM public.telegram_chat_group_members gm JOIN public.telegram_chat_groups g ON g.id=gm.group_id WHERE gm.chat_id=c.chat_id AND gm.enabled AND g.reminder_list_instance_id IS NOT NULL AND g.reminder_list_instance_id<>%s) ORDER BY c.chat_id LIMIT %s',
                    (int(after_id or '-9223372036854775808'),self.instance,limit+101))
                rows=[r for r in rows if int(r.get('id')) not in excluded_ids and _source_name(r.get('chat_name')) not in excluded_names][:limit+1]
            elif kind=='dialog_candidates':
                row=tx.one("SELECT details FROM public.integration_health WHERE instance_id=%s AND service_name='telegram-collector'",(self.instance,))
                details=(row or {}).get('details') or {}
                items=list(details.get('dialog_candidates') or [])
                allowed={'chat_id','name','kind','username','forum','topic_titles','last_message_at','last_message_preview'}
                items=[{k:v for k,v in item.items() if k in allowed} for item in items if isinstance(item,dict)]
                excluded_names,excluded_ids=self._source_exclusions(tx)
                items=[x for x in items if int(x.get('chat_id')) not in excluded_ids and _source_name(x.get('name')) not in excluded_names]
                if status=='groups_only':
                    items=[x for x in items if x.get('kind') in ('group','supergroup','forum')]
                return {'items':items[:limit],'scanned_at':details.get('dialog_candidates_scanned_at'),'collector_runtime':details.get('runtime_location'),'media_processed_for_candidates':False}
            elif kind=='reminder_lists':
                name=calendar.api_user(tx,self.instance)
                rows=tx.all("SELECT c.id,c.title,c.timezone,c.revision,m.role,COALESCE(c.props->>'ICAL:calendar-color',c.props->>'{http://apple.com/ns/ical/}calendar-color') AS color FROM tm_calendar.calendars c JOIN tm_calendar.members m ON m.calendar_id=c.id WHERE m.username=%s AND m.status='active' AND c.deleted_at IS NULL AND c.component_type='VTODO' AND (%s::uuid IS NULL OR c.id>%s::uuid) ORDER BY c.id LIMIT %s",(name,after_id,after_id,limit+1))
            elif kind=='reminders':
                name=calendar.api_user(tx,self.instance);c=calendar.access(tx,name,resource_id)
                if c.get('component_type')!='VTODO':raise Rejected('not_reminder_list')
                rows=tx.all("SELECT id,calendar_id AS list_id,caldav_uid,fields,revision,etag,sync_state FROM tm_calendar.events WHERE calendar_id=%s::uuid AND deleted_at IS NULL AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",(resource_id,after_id,after_id,limit+1))
            elif kind=='reminder':
                row=calendar.event(tx,calendar.api_user(tx,self.instance),resource_id)
                if row.get('component_type')!='VTODO':raise Rejected('not_reminder')
                return {'reminder':calendar.event_public(row)}
            elif kind=='rcc_settlement_summary':
                if not workspace_id:raise Rejected('workspace_id_required')
                from ..v75.settlement import read_summary
                return read_summary(tx,self.instance,workspace_id)
            elif kind=='project_balance':
                t=scope.task(tx,self.instance,resource_id)
                if t['record_kind']!='project':raise Rejected('financial_project_required')
                rows=tx.all('SELECT p.id,p.kind,p.state,a.amount,public.tm_evidence_valid_v14(p.group_id,p.evidence) AS evidence_valid FROM public.tm_payment_events p JOIN public.tm_payment_allocations a ON a.payment_id=p.id WHERE a.task_id=%s ORDER BY p.id,a.work_key',(t['id'],))
                stored=t['project_data'].get('finance_cost') or {}
                fin=finance.balance(stored.get('work_amount',t['project_data'].get('amount_rub')),rows)
                expected=tx.all("SELECT id,amount,expected_at,status,revision FROM tm_config.payment_expectations WHERE instance_id=%s AND task_id=%s ORDER BY created_at,id",(self.instance,t['id']))
                return {'task_id':t['id'],'balance':fin,'document_amounts':stored,'ledger':rows,'expectations':expected,'payment_window':t['project_data'].get('payment_window'),'expectations_in_received_net':False,'canonical_payment_status':t['payment_status'],'ledger_reconciliation_required':t['payment_status'] in ('paid','partial') and t['payment_status']!=fin['payment_status']}
            else:raise Rejected('unknown_configuration_read')
            return {'items':rows[:limit],'next_after_id':str(rows[limit-1]['id']) if len(rows)>limit else None}
