"""Read-only V25 schema/privilege probe; never installs or modifies schema."""
import json
import os
from tm_api.v24.database import Database
from tm_api.v24.preflight import check as check_v24

TABLES={
 'tm_config.workspaces':'id instance_id workspace_key name group_id status settings revision',
 'tm_config.documents':'id instance_id workspace_id document_key kind body status origin review_id evidence revision',
 'tm_config.defaults':'key body revision',
 'tm_config.history':'resource_type resource_id revision snapshot evidence operation',
 'tm_config.entities':'id instance_id workspace_id entity_type data status revision',
 'tm_config.analysis_receipts':'instance_id workspace_id configuration_token rule_refs reason',
 'tm_config.payment_details':'payment_id work_amount tax_amount document_total',
 'tm_config.payment_expectations':'id instance_id task_id amount expected_at status evidence revision',
 'tm_config.reminder_bindings':'task_id resource_id workspace_id base_title base_description manual_title manual_description hidden',
 'tm_calendar.calendars':'id title component_type props revision',
 'tm_calendar.events':'id calendar_id caldav_uid component_type fields icalendar etag revision deleted_at',
}
WRITABLE={
 'tm_config.workspaces':'INSERT,UPDATE', 'tm_config.documents':'INSERT,UPDATE',
 'tm_config.entities':'INSERT,UPDATE', 'tm_config.payment_expectations':'INSERT,UPDATE',
 'tm_config.reminder_bindings':'INSERT,UPDATE','tm_config.history':'INSERT',
 'tm_config.analysis_receipts':'INSERT','tm_config.payment_details':'INSERT',
 'public.telegram_chat_groups':'INSERT,UPDATE','public.telegram_chat_group_members':'INSERT,UPDATE',
 'public.telegram_chat_group_targets':'INSERT,UPDATE','public.tm_payment_events':'INSERT',
 'public.tm_payment_allocations':'INSERT',
}

def check(db):
    base=check_v24(db,extensions=True);missing=list(base['missing'])
    with db.transaction(read_only=True) as tx:
        for name,columns in TABLES.items():
            schema,table=name.split('.')
            actual={r['column_name'] for r in tx.all('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s',(schema,table))}
            missing.extend(name+'.'+c for c in columns.split() if c not in actual)
        for name,privileges in WRITABLE.items():
            exists=tx.one('SELECT to_regclass(%s) IS NOT NULL AS ok',(name,))['ok']
            if not exists:continue
            # Each privilege is required; SQL's comma-list form tests ANY, not ALL.
            for privilege in privileges.split(','):
                if not tx.one('SELECT has_table_privilege(current_user,%s,%s) AS ok',(name,privilege))['ok']:
                    missing.append('privilege:'+name+':'+privilege)
        if tx.one("SELECT to_regclass('tm_v24.migrations') IS NOT NULL AS ok")['ok']:
            versions={r['version'] for r in tx.all('SELECT version FROM tm_v24.migrations')}
            for n in (24,25):
                if n not in versions:missing.append('migration:'+str(n))
        trigger=tx.one("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='public.tasks'::regclass AND tgname='tm_v25_server_task_guard' AND tgenabled<>'D') AS ok")['ok']
        if not trigger:missing.append('server_task_writer_guard')
        role=tx.one('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
        if any(role.values()):missing.append('api_login_must_not_be_privileged')
    return {'ok':not missing,'version':'V25','missing':missing,'read_only':True,
        'helper_definitions_sha256':base['helper_definitions_sha256'],
        'functional_acceptance':'not_performed_by_preflight'}

def main():
    try:result=check(Database(os.environ.get('TM_V24_DSN','')))
    except Exception:result={'ok':False,'error':'preflight_unavailable','read_only':True}
    print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['ok'] else 1)

if __name__=='__main__':main()
