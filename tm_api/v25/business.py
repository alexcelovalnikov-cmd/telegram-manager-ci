"""Workspace and rule/configuration primitives; reuse immutable preview/apply.

An aggregate create_workspace includes its chat set, destination and documents
in one preview/transaction. A workspace is NOT a paid job in public.tasks.
"""
from copy import deepcopy
from uuid import uuid5,NAMESPACE_URL
from tm_api.v24.common import Rejected,check_revision,encoded,strict_fields
from tm_calendar import repository as calendars
from . import repository as repo
from .configuration import Workspace,Document,Destination,SourceSelector,validate_analysis,validate_formatting,validate_integrations,validate_entity

CONFIG_OPS={'create_workspace','update_workspace','migrate_workspace_storage','archive_workspace','delete_workspace','restore_workspace',
 'create_rule','update_rule','disable_rule','activate_rule','delete_rule','restore_rule_version',
 'create_template','update_template','delete_template','create_entity_type','update_entity_type'}
ENTITY_OPS={'create_managed_entity','update_managed_entity','archive_managed_entity'}

def stable(instance,key):return str(uuid5(NAMESPACE_URL,'tm-v25:'+instance+':'+key))

def rules_profile(settings,fallback='default'):
    profile=((settings or {}).get('analysis') or {}).get('profile')
    if profile=='postproduction':return 'postproduction'
    if profile=='rcc_settlement':return 'projects_payments'
    return fallback

def document_record(d):
    return {k:d[k] for k in ('id','instance_id','workspace_id','document_key','kind','body','status','origin','review_id','evidence','revision')}

class ConfigBusiness:
    def __init__(self,instance):self.instance=instance
    def _destination(self,tx,value,ident,generation=1):
        d=Destination.model_validate(value).model_dump();mode=d['mode']
        if mode in ('server_new','server_existing'):
            name=calendars.api_user(tx,self.instance)
            if mode=='server_new':
                d['id']=stable(self.instance,'workspace-list:'+ident+':'+str(generation))
                if tx.one('SELECT id FROM tm_calendar.calendars WHERE id=%s::uuid',(d['id'],)):raise Rejected('list_already_exists')
            else:
                c=calendars.access(tx,name,d['id'],write=True)
                if c.get('component_type')!='VTODO':raise Rejected('destination_is_not_reminder_list')
                d['title']=c['title']
        elif mode=='legacy_existing':
            row=tx.one("SELECT lists FROM public.tm_catalog_snapshots WHERE instance_id=%s AND checked_at>now()-interval '10 minutes'",(self.instance,))
            matches=[r for r in (row or {}).get('lists',[]) if r.get('id')==d['id'] and r.get('writable') is True]
            if len(matches)!=1:raise Rejected('refresh_native_list_catalog_first')
            d['title']=matches[0]['name']
        return d
    def _calendar(self,tx,ident):
        if ident:
            c=calendars.access(tx,calendars.api_user(tx,self.instance),ident,write=True)
            if c.get('component_type','VEVENT')!='VEVENT':raise Rejected('calendar_is_not_event_collection')
    def _check_conflicts(self,tx,workspace_id,doc,ignore=None):
        if doc['status']!='active':return
        rows=tx.all("SELECT id,kind,body FROM tm_config.documents WHERE instance_id=%s AND workspace_id IS NOT DISTINCT FROM %s::uuid AND status='active' AND (%s::uuid IS NULL OR id<>%s::uuid)",(self.instance,workspace_id,ignore,ignore))
        for old in rows:
            if old['kind']!=doc['kind']:continue
            a,b=old['body'],doc['body']
            if doc['kind']=='rule' and b.get('decision_key') and a.get('decision_key')==b['decision_key'] and a.get('value')!=b.get('value'):
                raise Rejected('rule_conflict_update_existing_rule_or_disable_it_first')
            if doc['kind']=='template' and (a['entity_kind'],a['channel'])==(b['entity_kind'],b['channel']):
                raise Rejected('template_already_exists_update_it')
    def plan(self,tx,m,lock=False):
        op=m['operation'];patch=deepcopy(m['changes']);repo.explicit(m) if op in CONFIG_OPS else None
        ident=stable(self.instance,m['dedupe_key']) if op.startswith('create_') else m['target_id']
        if op in ENTITY_OPS:return self.entity_plan(tx,m,ident,lock)
        before=None;extra={}
        if op=='create_workspace':
            value=Workspace.model_validate(patch).model_dump()
            if tx.one('SELECT id FROM tm_config.workspaces WHERE instance_id=%s AND workspace_key=%s',(self.instance,value['key'])):raise Rejected('workspace_key_exists')
            chats=repo.read_chats(tx,self.instance,value['chat_ids'])
            group=None
            if value['existing_group_key']:
                group=tx.one('SELECT * FROM public.telegram_chat_groups WHERE group_key=%s AND reminder_list_instance_id=%s',(value['existing_group_key'],self.instance))
                if not group:raise Rejected('existing_group_not_authorized')
                if tx.one('SELECT id FROM tm_config.workspaces WHERE group_id=%s',(group['id'],)):raise Rejected('group_already_managed')
                if value['reminder_list']['mode'] not in ('none','legacy_existing'):raise Rejected('existing_native_group_needs_explicit_migration_not_adoption')
                saved=tx.all('SELECT chat_id FROM public.telegram_chat_group_members WHERE group_id=%s AND enabled ORDER BY chat_id',(group['id'],))
                if sorted(value['chat_ids'])!=[r['chat_id'] for r in saved]:raise Rejected('adopt_existing_chat_set_first')
                if value['reminder_list']['mode']=='none':
                    value['reminder_list']={'mode':'legacy_existing','id':group['reminder_list_id'],'title':group['reminder_list_name']}
            dest=self._destination(tx,value['reminder_list'],ident)
            if group and dest.get('id')!=group.get('reminder_list_id'):
                raise Rejected('adopt_existing_destination_before_reassigning')
            self._calendar(tx,value['calendar_id']);value['reminder_list']=dest
            documents=value.pop('documents')
            seen_decisions={};seen_templates=set()
            for d in documents:
                if d['origin']=='review_generalization':d['status']='proposed'
                if d['status']=='active' and d['kind']=='rule' and d['body'].get('decision_key'):
                    key=d['body']['decision_key']
                    if key in seen_decisions and seen_decisions[key]!=d['body'].get('value'):raise Rejected('conflicting_initial_rules')
                    seen_decisions[key]=d['body'].get('value')
                if d['status']=='active' and d['kind']=='template':
                    key=(d['body']['entity_kind'],d['body']['channel'])
                    if key in seen_templates:raise Rejected('conflicting_initial_templates')
                    seen_templates.add(key)
            after={'id':ident,'instance_id':self.instance,'workspace_key':value.pop('key'),'name':value.pop('name'),'status':'active',
                   'group_id':group['id'] if group else None,'settings':value,'revision':1,'initial_documents':documents}
            extra={'group_before':group,'resolved_chats':chats}
        elif op=='migrate_workspace_storage':
            w=repo.workspace(tx,self.instance,ident,lock);check_revision(w['revision'],m['expected_revision'])
            before=deepcopy(w);after=deepcopy(w)
            if w['status']!='active':raise Rejected('restore_workspace_before_migration')
            strict_fields(patch,('reminder_list_title','migrate_calendar'),('reminder_list_title',))
            title=patch['reminder_list_title']
            if not isinstance(title,str) or not 1<=len(title)<=200:raise Rejected('invalid_list_title')
            migrate_calendar=patch.get('migrate_calendar',True)
            if type(migrate_calendar) is not bool:raise Rejected('invalid_migrate_calendar')
            settings=after['settings']
            if settings['reminder_list']['mode'] not in ('legacy_existing','none'):
                raise Rejected('workspace_storage_already_server_managed')
            if migrate_calendar and not settings.get('calendar_id'):
                raise Rejected('workspace_calendar_required')
            dest=self._destination(tx,{'mode':'server_new','title':title},ident,generation=w['revision']+1)
            tasks=tx.all(
                "SELECT id,title,description,status,record_kind,target_app,reminder_active,apple_reminder_id,"
                "reminder_due_spec,due_at,tags,calendar_sync_title,calendar_sync_description,"
                "calendar_sync_start_at,calendar_sync_end_at,apple_calendar_event_id,project_data "
                "FROM public.tasks WHERE context_group_id=%s ORDER BY id LIMIT 1001",
                (w['group_id'],))
            if len(tasks)>1000:raise Rejected('workspace_migration_limit')
            if tx.one("SELECT 1 AS found FROM tm_config.reminder_bindings b JOIN public.tasks t ON t.id=b.task_id WHERE t.context_group_id=%s LIMIT 1",(w['group_id'],)):
                raise Rejected('workspace_already_has_server_bindings')
            reminders=[];calendar_events=[];skipped=[]
            for task in tasks:
                if task.get('target_app')=='calendar':
                    if migrate_calendar and task.get('calendar_sync_start_at') and task.get('calendar_sync_end_at'):
                        calendar_events.append(task)
                    else:skipped.append({'id':task['id'],'reason':'calendar_fields_incomplete_or_disabled'})
                elif task.get('apple_reminder_id') or task.get('record_kind')=='project' or (task.get('target_app')=='reminders' and task.get('reminder_active')):
                    reminders.append(task)
                else:skipped.append({'id':task['id'],'reason':'not_currently_projected'})
            for task in reminders:
                rid=str(uuid5(NAMESPACE_URL,'tm-task:'+self.instance+':'+str(task['id'])))
                if tx.one('SELECT id FROM tm_calendar.events WHERE id=%s::uuid',(rid,)):
                    raise Rejected('server_reminder_identity_already_exists')
            for task in calendar_events:
                eid=str(uuid5(NAMESPACE_URL,'tm-task-calendar:'+self.instance+':'+str(task['id'])))
                if tx.one('SELECT id FROM tm_calendar.events WHERE id=%s::uuid',(eid,)):
                    raise Rejected('server_calendar_identity_already_exists')
            settings['reminder_list']=dest;after['revision']+=1
            after['storage_migration_preview']={
                'reminder_list':dest,
                'reminders':[{'id':t['id'],'title':t['title'],'status':t['status']} for t in reminders],
                'calendar_events':[{'id':t['id'],'title':t.get('calendar_sync_title') or t['title'],
                                    'start_at':t['calendar_sync_start_at'],'end_at':t['calendar_sync_end_at']} for t in calendar_events],
                'skipped':skipped}
            extra={'group_before':tx.one('SELECT * FROM public.telegram_chat_groups WHERE id=%s'+(' FOR SHARE' if lock else ''),(w['group_id'],)),
                   'migration_reminders':reminders,'migration_calendar_events':calendar_events}
        elif op in ('update_workspace','archive_workspace','delete_workspace','restore_workspace'):
            w=repo.workspace(tx,self.instance,ident,lock);check_revision(w['revision'],m['expected_revision']);before=deepcopy(w);after=deepcopy(w)
            strict_fields(patch,('name','chat_ids','source_selectors','default_tags','reminder_list','calendar_id','analysis','formatting','integrations','timezone') if op=='update_workspace' else ())
            if op!='update_workspace':after['status']={'archive_workspace':'archived','delete_workspace':'deleted','restore_workspace':'active'}[op]
            else:
                if w['status']!='active':raise Rejected('restore_workspace_before_editing')
                if 'name' in patch:
                    if not isinstance(patch['name'],str) or not 1<=len(patch['name'])<=200:raise Rejected('invalid_workspace_name')
                    after['name']=patch.pop('name')
                s=after['settings']
                if 'chat_ids' in patch:
                    ids=patch['chat_ids']
                    if not isinstance(ids,list) or len(ids)>200 or any(type(x) is not int or x==0 for x in ids) or len(set(ids))!=len(ids):raise Rejected('invalid_chat_set')
                    extra['resolved_chats']=repo.read_chats(tx,self.instance,ids)
                if 'reminder_list' in patch:
                    dest=self._destination(tx,patch['reminder_list'],ident,generation=w['revision']+1)
                    if dest['id']!=s['reminder_list'].get('id'):
                        tasks=tx.all('SELECT t.id,t.updated_at,b.resource_id,e.calendar_id,e.href,e.caldav_uid,e.revision,e.deleted_at FROM public.tasks t LEFT JOIN tm_config.reminder_bindings b ON b.task_id=t.id LEFT JOIN tm_calendar.events e ON e.id=b.resource_id WHERE t.context_group_id=%s ORDER BY t.id LIMIT 501',(w['group_id'],))
                        if len(tasks)>500:raise Rejected('workspace_move_limit_split_with_explicit_migration')
                        if tasks and (s['reminder_list']['mode'] not in ('server_new','server_existing') or dest['mode'] not in ('server_new','server_existing') or any(not t['resource_id'] for t in tasks)):
                            raise Rejected('legacy_destination_change_requires_verified_import_and_cutover')
                        for t in tasks:
                            if not t['deleted_at'] and tx.one('SELECT id FROM tm_calendar.events WHERE calendar_id=%s::uuid AND (href=%s OR caldav_uid=%s)',(dest['id'],t['href'],t['caldav_uid'])):
                                raise Rejected('destination_contains_duplicate_reminder')
                        extra['reminders_to_move']=tasks
                    patch['reminder_list']=dest
                if 'calendar_id' in patch:self._calendar(tx,patch['calendar_id'])
                if 'source_selectors' in patch:
                    if not isinstance(patch['source_selectors'],list) or len(patch['source_selectors'])>200:raise Rejected('invalid_source_selectors')
                    patch['source_selectors']=[SourceSelector.model_validate(x).model_dump() for x in patch['source_selectors']]
                    if len({(x['kind'],x['value']) for x in patch['source_selectors']})!=len(patch['source_selectors']):raise Rejected('duplicate_source_selector')
                if 'default_tags' in patch:
                    if not isinstance(patch['default_tags'],list) or len(patch['default_tags'])>30 or any(not isinstance(x,str) or not x or len(x)>100 for x in patch['default_tags']):raise Rejected('invalid_default_tags')
                if 'analysis' in patch:validate_analysis(patch['analysis'])
                if 'formatting' in patch:validate_formatting(patch['formatting'])
                if 'integrations' in patch:validate_integrations(patch['integrations'])
                if 'timezone' in patch:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(patch['timezone'])
                s.update(patch)
            after['revision']+=1
            extra['group_before']=tx.one('SELECT * FROM public.telegram_chat_groups WHERE id=%s'+(' FOR SHARE' if lock else ''),(w['group_id'],))
        else:
            creating=op in ('create_rule','create_template','create_entity_type')
            if creating:
                kind={'create_rule':'rule','create_template':'template','create_entity_type':'entity_type'}[op]
                d=Document.model_validate(patch|{'kind':kind}).model_dump()
                if m.get('workspace_id'):repo.workspace(tx,self.instance,m['workspace_id'],active=True)
                if d['origin']=='review_generalization':d['status']='proposed'
                if tx.one('SELECT id FROM tm_config.documents WHERE instance_id=%s AND workspace_id IS NOT DISTINCT FROM %s::uuid AND document_key=%s',(self.instance,m.get('workspace_id'),d['key'])):raise Rejected('document_key_exists')
                after={'id':ident,'instance_id':self.instance,'workspace_id':m.get('workspace_id'),'document_key':d.pop('key'),**d,'revision':1,'evidence':m['evidence']}
            else:
                row=tx.one('SELECT * FROM tm_config.documents WHERE id=%s::uuid AND instance_id=%s'+(' FOR UPDATE' if lock else ''),(ident,self.instance))
                if not row:raise Rejected('configuration_document_not_found')
                check_revision(row['revision'],m['expected_revision']);before=document_record(row);after=deepcopy(before)
                if m.get('workspace_id') not in (None,row['workspace_id']):raise Rejected('workspace_target_mismatch')
                expected_kind='template' if 'template' in op else 'entity_type' if 'entity_type' in op else 'rule'
                if row['kind']!=expected_kind:raise Rejected('configuration_document_kind_mismatch')
                if op in ('disable_rule','activate_rule','delete_rule','delete_template'):
                    strict_fields(patch,());after['status']={'disable_rule':'disabled','activate_rule':'active','delete_rule':'deleted','delete_template':'deleted'}[op]
                elif op=='restore_rule_version':
                    strict_fields(patch,('version',),('version',))
                    version=patch['version']
                    if type(version) is not int or version<1:raise Rejected('invalid_rule_version')
                    old=tx.one("SELECT snapshot FROM tm_config.history WHERE instance_id=%s AND resource_type='document' AND resource_id=%s::uuid AND revision=%s",(self.instance,ident,version))
                    if not old:raise Rejected('historical_rule_version_not_found')
                    after['body']=old['snapshot']['body'];after['status']='active'
                else:
                    strict_fields(patch,('body','status'))
                    after.update(patch)
                value=Document.model_validate({'key':row['document_key'],'kind':row['kind'],'body':after['body'],'status':after['status'],'origin':row['origin'],'review_id':row.get('review_id')})
                after['body']=value.body;after['revision']+=1;after['evidence']=m['evidence']
            self._check_conflicts(tx,after['workspace_id'],after,ignore=None if creating else ident)
        if extra.get('reminders_to_move'):after['reminder_move_preview']=extra['reminders_to_move']
        return {'entity':'workspace' if 'workspace' in op else 'configuration_document','operation':op,'target_id':ident,'before':before,'after':after,**extra}
    def _save_document(self,tx,d,m):
        tx.execute('INSERT INTO tm_config.documents(id,instance_id,workspace_id,document_key,kind,body,status,origin,review_id,evidence,revision) VALUES(%s::uuid,%s,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(id) DO UPDATE SET body=EXCLUDED.body,status=EXCLUDED.status,evidence=EXCLUDED.evidence,revision=EXCLUDED.revision,updated_at=clock_timestamp()',
           (d['id'],self.instance,d['workspace_id'],d['document_key'],d['kind'],encoded(d['body']),d['status'],d['origin'],d.get('review_id'),encoded(m['evidence']),d['revision']))
        repo.history(tx,self.instance,'document',d['id'],d['revision'],d,m['evidence'],m['operation'])
    def execute(self,tx,m,plan):
        if m['operation'] in ENTITY_OPS:return self.entity_execute(tx,m,plan)
        a=deepcopy(plan['after']);a.pop('reminder_move_preview',None);a.pop('storage_migration_preview',None);ident=a['id'];op=m['operation'];repo.actor(tx,self.instance)
        if plan['entity']=='configuration_document':
            self._save_document(tx,a,m)
        else:
            s=a['settings'];dest=s['reminder_list'];new=op=='create_workspace';g=plan.get('group_before')
            if dest['mode']=='server_new' and (new or (plan['before']['settings']['reminder_list'].get('id')!=dest['id'])):
                calendars.create(tx,calendars.api_user(tx,self.instance),dest['title'],s['timezone'],dest['id'],component_type='VTODO')
            legacy=dest['mode']=='legacy_existing'
            if new and not g:
                profile=rules_profile(s,'default')
                g=tx.one('INSERT INTO public.telegram_chat_groups(group_key,name,enabled,monitoring_enabled,rules_profile,reminder_list_instance_id,reminder_list_id,reminder_list_name,import_existing_reminders) VALUES(%s,%s,true,true,%s,%s,%s,%s,%s) RETURNING *',
                    ('workspace-'+ident,a['name'],profile,self.instance,dest['id'] if legacy else None,dest['title'] if legacy else None,legacy))
                a['group_id']=g['id']
            elif not new:
                profile=rules_profile(s,g.get('rules_profile','default')) if 'analysis' in m['changes'] else g.get('rules_profile','default')
                tx.execute('UPDATE public.telegram_chat_groups SET name=%s,enabled=%s,monitoring_enabled=%s,rules_profile=%s,updated_at=clock_timestamp() WHERE id=%s',
                           (a['name'],a['status']=='active',a['status']=='active',profile,a['group_id']))
                if 'reminder_list' in m['changes'] or op=='migrate_workspace_storage':
                    tx.execute('UPDATE public.telegram_chat_groups SET reminder_list_id=%s,reminder_list_name=%s,import_existing_reminders=%s,updated_at=clock_timestamp() WHERE id=%s',(dest['id'] if legacy else None,dest.get('title') if legacy else None,legacy,a['group_id']))
            if new:
                tx.execute('INSERT INTO tm_config.workspaces(id,instance_id,workspace_key,name,group_id,status,settings) VALUES(%s::uuid,%s,%s,%s,%s,%s,%s::jsonb)',
                    (ident,self.instance,a['workspace_key'],a['name'],a['group_id'],a['status'],encoded(s)))
            else:
                tx.execute('UPDATE tm_config.workspaces SET name=%s,status=%s,settings=%s::jsonb,revision=%s,updated_at=clock_timestamp() WHERE id=%s::uuid',
                    (a['name'],a['status'],encoded(s),a['revision'],ident))
            for reminder in plan.get('reminders_to_move',[]):
                if reminder['deleted_at']:continue
                tx.execute('UPDATE tm_calendar.events SET calendar_id=%s::uuid,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s::uuid',(dest['id'],reminder['resource_id']))
                calendars.changed(tx,reminder['calendar_id'],reminder['href'],True)
                calendars.changed(tx,dest['id'],reminder['href'],False)
                calendars.calendar_audit(tx,calendars.api_user(tx,self.instance),'move_workspace_reminder',reminder['resource_id'],{'list_id':reminder['calendar_id']},{'list_id':dest['id']})
            if op=='migrate_workspace_storage':
                from tm_reminders.models import Reminder
                from tm_reminders import codec as reminder_codec
                from tm_api.v24.models import Event
                from tm_calendar import codec as event_codec
                user=calendars.api_user(tx,self.instance)
                for task in plan.get('migration_reminders',[]):
                    rid=str(uuid5(NAMESPACE_URL,'tm-task:'+self.instance+':'+str(task['id'])))
                    spec=task.get('reminder_due_spec') or {};due=task.get('due_at');all_day=False
                    if spec.get('kind')=='date' and spec.get('date'):
                        due=spec['date'];all_day=True
                    fields=Reminder(
                        title=task['title'],description=task.get('description') or '',due_at=due,all_day=all_day,
                        timezone=s['timezone'],status={'completed':'completed','cancelled':'cancelled','waiting':'in_progress'}.get(task['status'],'open'),
                        categories=[x for x in (task.get('tags') or []) if x]).model_dump()
                    uid=rid+'@telegram-manager';text=reminder_codec.encode(fields,uid)
                    calendars.put(tx,user,dest['id'],rid+'.ics',text,ident=rid,source='telegram_manager')
                    tx.execute('INSERT INTO tm_config.reminder_bindings(task_id,resource_id,workspace_id,base_title,base_description) VALUES(%s,%s::uuid,%s::uuid,%s,%s)',
                               (task['id'],rid,ident,task['title'],task.get('description') or ''))
                    tx.execute("UPDATE public.tasks SET target_app=NULL,reminder_active=false,external_system='tm_server_reminders',external_id=%s,updated_at=clock_timestamp() WHERE id=%s",
                               (rid,task['id']))
                for task in plan.get('migration_calendar_events',[]):
                    eid=str(uuid5(NAMESPACE_URL,'tm-task-calendar:'+self.instance+':'+str(task['id'])))
                    fields=Event(
                        title=task.get('calendar_sync_title') or task['title'],
                        description=task.get('calendar_sync_description') or task.get('description') or '',
                        location='',start_at=task['calendar_sync_start_at'],end_at=task['calendar_sync_end_at'],
                        timezone=s['timezone'],all_day=False,recurrence=None,
                        status='cancelled' if task['status']=='cancelled' else 'confirmed').model_dump()
                    uid=eid+'@telegram-manager';text=event_codec.encode(fields,uid)
                    calendars.put(tx,user,s['calendar_id'],eid+'.ics',text,ident=eid,source='telegram_manager')
                    tx.execute("UPDATE public.tasks SET target_app=NULL,external_system='tm_server_calendar',external_id=%s,updated_at=clock_timestamp() WHERE id=%s",
                               (eid,task['id']))
            if new or 'chat_ids' in m['changes']:
                tx.execute('UPDATE public.telegram_chat_group_members SET enabled=false WHERE group_id=%s AND NOT managed_by_targets AND NOT(chat_id=ANY(%s::bigint[]))',(a['group_id'],s['chat_ids']))
                for chat in s['chat_ids']:
                    tx.execute('INSERT INTO public.telegram_chat_group_members(group_id,chat_id,enabled,managed_by_targets) VALUES(%s,%s,true,false) ON CONFLICT(group_id,chat_id) DO UPDATE SET enabled=true,managed_by_targets=false',(a['group_id'],chat))
            if (new or 'chat_ids' in m['changes'] or 'source_selectors' in m['changes']) and rules_profile(s,g.get('rules_profile','default') if g else 'default')=='postproduction':
                tx.execute("UPDATE public.telegram_chat_group_targets SET enabled=false WHERE group_id=%s AND enabled AND selector_kind='title' AND selector_value LIKE 'dynamic:%%'",(a['group_id'],))
            if new or 'source_selectors' in m['changes']:
                desired={(x['kind'],x['value']) for x in s.get('source_selectors',[])}
                for old in tx.all('SELECT id,selector_kind,selector_value FROM public.telegram_chat_group_targets WHERE group_id=%s AND enabled',(a['group_id'],)):
                    if str(old['selector_value']).startswith('dynamic:'):
                        continue
                    if (old['selector_kind'],old['selector_value']) not in desired:
                        tx.execute('UPDATE public.telegram_chat_group_targets SET enabled=false WHERE id=%s',(old['id'],))
                for kind,value in sorted(desired):
                    tx.execute('INSERT INTO public.telegram_chat_group_targets(group_id,selector_kind,selector_value,enabled) VALUES(%s,%s,%s,true) ON CONFLICT(group_id,selector_kind,selector_value) DO UPDATE SET enabled=true',(a['group_id'],kind,value))
            for d in a.pop('initial_documents',[]):
                self._save_document(tx,{'id':stable(self.instance,ident+':'+d['key']),'instance_id':self.instance,'workspace_id':ident,'document_key':d.pop('key'),**d,'evidence':m['evidence'],'revision':1},m)
            repo.history(tx,self.instance,'workspace',ident,a['revision'],a,m['evidence'],op)
        result={'id':ident,'entity':plan['entity'],'revision':a['revision'],'status':a['status'],'existing_data_deleted':False,
                'telegram_source_resolution':'collector_resolves_pending_selectors' if a.get('settings',{}).get('source_selectors') else 'explicit_chat_ids'}
        if op=='migrate_workspace_storage':
            result['storage_migration']={'reminders':len(plan.get('migration_reminders',[])),
                                         'calendar_events':len(plan.get('migration_calendar_events',[])),
                                         'mac_dependency_removed':True}
        return result
    def _validate_entity_hierarchy(self,tx,a):
        parent=(a.get('data') or {}).get('parent_id')
        if not parent:
            return
        if parent==a['id']:
            raise Rejected('managed_entity_cannot_parent_itself')
        seen={a['id']}
        current=parent
        for _ in range(32):
            if current in seen:
                raise Rejected('managed_entity_parent_cycle')
            seen.add(current)
            row=tx.one('SELECT id,data,status FROM tm_config.entities WHERE id=%s::uuid AND instance_id=%s AND workspace_id=%s::uuid',
                       (current,self.instance,a['workspace_id']))
            if not row or row['status']!='active':
                raise Rejected('active_parent_entity_required')
            current=(row.get('data') or {}).get('parent_id')
            if not current:
                return
        raise Rejected('managed_entity_hierarchy_too_deep')

    def entity_plan(self,tx,m,ident,lock):
        ctx=repo.guard_token(tx,self.instance,m)
        if not ctx:raise Rejected('workspace_and_configuration_token_required')
        creating=m['operation']=='create_managed_entity';before=None
        if creating:
            strict_fields(m['changes'],('entity_type','data'),('entity_type','data'))
            a={'id':ident,'workspace_id':m['workspace_id'],'instance_id':self.instance,'entity_type':m['changes']['entity_type'],'data':m['changes']['data'],'status':'active','revision':1}
        else:
            a=tx.one('SELECT * FROM tm_config.entities WHERE id=%s::uuid AND instance_id=%s AND workspace_id=%s::uuid'+(' FOR UPDATE' if lock else ''),(ident,self.instance,m['workspace_id']))
            if not a:raise Rejected('managed_entity_not_found')
            check_revision(a['revision'],m['expected_revision']);before=deepcopy(a);a=deepcopy(a);a['revision']+=1
            if m['operation']=='archive_managed_entity':strict_fields(m['changes'],());a['status']='archived'
            else:
                strict_fields(m['changes'],('data',),('data',))
                a['data']=a['data']|m['changes']['data']
                # V70 removed logical sections entirely. Clean legacy metadata
                # whenever an existing managed entity is touched.
                a['data'].pop('section',None)
        definitions=[d for d in ctx['documents'] if d['kind']=='entity_type' and d['document_key']==a['entity_type'] and d['workspace_id']==m['workspace_id'] and d['status']=='active']
        if len(definitions)!=1:raise Rejected('active_entity_type_required')
        a['data']=validate_entity(definitions[0]['body'],a['data'])
        self._validate_entity_hierarchy(tx,a)
        if definitions[0]['body'].get('reminder_projection'):
            raise Rejected('use_explicit_create_reminder_for_custom_entity_projection')
        return {'entity':'managed_entity','operation':m['operation'],'target_id':ident,'before':before,'after':a,'configuration_token':ctx['configuration_token']}
    def entity_execute(self,tx,m,plan):
        a=plan['after']
        tx.execute('INSERT INTO tm_config.entities(id,instance_id,workspace_id,entity_type,data,status,revision) VALUES(%s::uuid,%s,%s::uuid,%s,%s::jsonb,%s,%s) ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data,status=EXCLUDED.status,revision=EXCLUDED.revision,updated_at=clock_timestamp()',
             (a['id'],self.instance,a['workspace_id'],a['entity_type'],encoded(a['data']),a['status'],a['revision']))
        repo.history(tx,self.instance,'entity',a['id'],a['revision'],a,m['evidence'],m['operation'])
        return {'entity':'managed_entity','id':a['id'],'revision':a['revision']}
