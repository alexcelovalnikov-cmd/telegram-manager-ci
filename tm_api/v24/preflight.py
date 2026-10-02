"""Read-only schema and permission checks; never install migrations automatically."""
import argparse,json,os
from .database import Database
from .common import digest

REQUIRED={
 'telegram_messages':'chat_id message_id date created_at text sender_id sender_name edited_at is_deleted reply_to_message_id media_source_token has_media search_vector',
 'telegram_attachments':'id chat_id message_id attachment_index source_token deleted_at extracted_text transcript content_summary processing_status source_available text_is_complete',
 'telegram_chats':'chat_id chat_name enabled',
 'telegram_chat_groups':'id group_key name enabled monitoring_enabled reminder_list_instance_id reminder_list_id rules_profile',
 'telegram_chat_group_members':'group_id chat_id enabled',
 'tasks':'id record_kind context_group_id title description updated_at status completed_at cancelled_at project_data project_archived_at payment_status payment_evidence payment_confirmed_at target_app tags due_at reminder_due_spec',
 'tm_project_links':'task_id chat_id message_id','tm_salary_periods_v15':'task_id','tm_salary_series_v15':'seed_task_id',
 'tm_payment_events':'id group_id kind amount state created_at','tm_payment_allocations':'payment_id task_id work_key amount',
 'tm_review_focus_state_v18':'instance_id scope_key review_id version','tm_review_items':'id status',
 'tm_project_import_state':'group_id instance_id list_id status last_snapshot_at',
}
FUNCTIONS=['tm_group_chat_allowed_v14','tm_content_token_v11','tm_payment_title_v18','tm_record_payment_v14','tm_evidence_valid_v14']

def check(db,extensions=False):
    missing=[]
    with db.transaction(read_only=True) as tx:
        for table,columns in REQUIRED.items():
            found={r['column_name'] for r in tx.all("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",(table,))}
            missing += [table+'.'+c for c in columns.split() if c not in found]
        rows=tx.all("SELECT p.proname,pg_get_functiondef(p.oid) AS definition,has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname=ANY(%s::text[]) ORDER BY p.proname",(FUNCTIONS,))
        found={r['proname'] for r in rows}
        missing += ['function:'+f for f in FUNCTIONS if f not in found]
        missing += ['execute:'+r['proname'] for r in rows if not r['can_execute']]
        if extensions:
            for schema,table in [('tm_v24','migrations'),('tm_v24','previews'),('tm_v24','operations'),('tm_v24','review_items'),('tm_calendar','calendars'),('tm_calendar','events'),('tm_calendar','members')]:
                if not tx.one('SELECT to_regclass(%s) IS NOT NULL AS ok',(schema+'.'+table,))['ok']:missing.append(schema+'.'+table)
        return {'ok':not missing,'missing':missing,'read_only':True,'helper_definitions_sha256':digest(rows),'notes':'Schema and privileges only; this is not a functional or device acceptance test.'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--extensions',action='store_true');args=p.parse_args()
    try:
        result=check(Database(os.environ.get('TM_V24_DSN','')),args.extensions)
        print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['ok'] else 1)
    except Exception:
        print('{"ok":false,"error":"preflight_unavailable"}');raise SystemExit(1) from None
if __name__=='__main__':main()
