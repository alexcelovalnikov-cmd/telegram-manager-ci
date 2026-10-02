"""Read-only server protocol and RPC-signature gate for Telegram Manager V18.

Never calls any listed mutating RPC. The server checks catalog permissions and
parameter names; optional DEFAULT parameters are supported. No secrets printed.
"""
from rr_common import INSTANCE_ID

PROTOCOL = 'v16_workflows_estimates_1'
REQUIREMENTS = [{'name': 'rr_commit_group_target_v7', 'args': ['p_candidates', 'p_chat_id', 'p_chat_name', 'p_error', 'p_retry_seconds', 'p_revision', 'p_status', 'p_target_id']}, {'name': 'rr_group_config_v7', 'args': []}, {'name': 'tm_adopt_salary_v16', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_instance_id', 'p_list_id', 'p_task_id']}, {'name': 'tm_analysis_window_v15', 'args': ['p_before_id', 'p_chat_id', 'p_group_key', 'p_limit']}, {'name': 'tm_assign_group_list_v8', 'args': ['p_command_id', 'p_expected_updated_at', 'p_group_key', 'p_instance_id', 'p_list_id', 'p_token']}, {'name': 'tm_claim_job_v15', 'args': ['p_instance_id', 'p_worker']}, {'name': 'tm_claim_v8', 'args': ['p_instance_id']}, {'name': 'tm_command_live_v8', 'args': ['p_id', 'p_instance_id', 'p_token']}, {'name': 'tm_commit_attachment_text_v10', 'args': ['p_chat_id', 'p_message_id', 'p_result', 'p_source_token']}, {'name': 'tm_commit_history_page_v15', 'args': ['p_cursor', 'p_entries', 'p_expected_offset', 'p_expected_read', 'p_job_id', 'p_page_key', 'p_scanned', 'p_stop_reason', 'p_token']}, {'name': 'tm_finish_job_v11', 'args': ['p_delay', 'p_error', 'p_id', 'p_progress', 'p_result', 'p_status', 'p_token']}, {'name': 'tm_finish_v8', 'args': ['p_error_code', 'p_id', 'p_instance_id', 'p_result', 'p_status', 'p_token']}, {'name': 'tm_import_projects_v15', 'args': ['p_group_key', 'p_instance_id', 'p_list_id', 'p_rows']}, {'name': 'tm_ingest_message_v10', 'args': ['p_attachment', 'p_delete', 'p_message']}, {'name': 'tm_mark_analysis_v11', 'args': ['p_chat_id', 'p_evidence', 'p_group_key']}, {'name': 'tm_media_batch_v16', 'args': ['p_instance_id', 'p_limit']}, {'name': 'tm_payment_evidence_current_v15', 'args': ['p_task_id']}, {'name': 'tm_project_ack_v16', 'args': ['p_expected_updated_at', 'p_instance_id', 'p_list_id', 'p_project_data', 'p_reminder_id', 'p_state', 'p_task_id']}, {'name': 'tm_project_delete_begin_v11', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_task_id']}, {'name': 'tm_project_delete_finish_v11', 'args': ['p_absence_verified', 'p_attempt_id', 'p_task_id']}, {'name': 'tm_propose_project_v11', 'args': ['p_chat_id', 'p_confidence', 'p_evidence', 'p_group_key', 'p_history_only', 'p_kind', 'p_matches', 'p_patch', 'p_request_key', 'p_task_id']}, {'name': 'tm_propose_task_v11', 'args': ['p_chat_id', 'p_confidence', 'p_description', 'p_evidence', 'p_group_key', 'p_history_only', 'p_message_id', 'p_title']}, {'name': 'tm_publish_catalog_v8', 'args': ['p_catalog', 'p_instance_id']}, {'name': 'tm_publish_review_v15', 'args': ['p_instance_id', 'p_items']}, {'name': 'tm_record_media_v16', 'args': ['p_attachment_id', 'p_content_token', 'p_instance_id', 'p_parsed', 'p_parser_version']}, {'name': 'tm_register_reply_v10', 'args': ['p_chat_id', 'p_reply_message_id']}, {'name': 'tm_retry_attachment_v10', 'args': ['p_chat_id', 'p_message_id']}, {'name': 'tm_review_context_v16', 'args': ['p_instance_id']}, {'name': 'tm_review_due_v15', 'args': ['p_explicit', 'p_limit']}, {'name': 'tm_salary_context_v15', 'args': ['p_group_id', 'p_instance_id', 'p_list_id']}, {'name': 'tm_salary_delete_begin_v15', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_task_id']}, {'name': 'tm_salary_delete_finish_v15', 'args': ['p_absence_verified', 'p_attempt_id', 'p_task_id']}, {'name': 'tm_salary_observe_v16', 'args': ['p_expected_updated_at', 'p_instance_id', 'p_list_id', 'p_observation', 'p_task_id']}, {'name': 'tm_salary_payment_current_v15', 'args': ['p_task_id']}, {'name': 'tm_salary_register_v16', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_instance_id', 'p_list_id', 'p_start_date', 'p_task_id']}, {'name': 'tm_salary_roll_v16', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_instance_id', 'p_list_id', 'p_task_id']}, {'name': 'tm_workflow_complete_v16', 'args': ['p_expected_updated_at', 'p_fingerprint', 'p_instance_id', 'p_list_id', 'p_task_id']}, {'name': 'tm_workflow_context_v16', 'args': ['p_instance_id']}]


REQUIREMENTS.extend([{'name': 'tm_personal_contract_v18', 'args': ['p_instance_id']}, {'name': 'tm_review_bundle_v18', 'args': ['p_instance_id', 'p_explicit', 'p_limit']}, {'name': 'tm_record_runtime_events_v18', 'args': ['p_instance_id', 'p_events']}, {'name': 'tm_project_ack_v18', 'args': ['p_task_id', 'p_expected_updated_at', 'p_instance_id', 'p_list_id', 'p_reminder_id', 'p_state', 'p_project_data']}])

REQUIREMENTS.extend([
 {'name':'tm_salary_roll_v18','args':['p_task_id','p_instance_id','p_list_id','p_expected_updated_at','p_fingerprint']},
 {'name':'tm_review_context_v18','args':['p_instance_id']},
 {'name':'tm_review_focus_get_v18','args':['p_instance_id','p_scope_key']},
 {'name':'tm_review_focus_set_v18','args':['p_instance_id','p_scope_key','p_review_id','p_review_revision','p_expected_version']},
 {'name':'tm_review_focus_reply_v18','args':['p_instance_id','p_scope_key','p_expected_version','p_request_key','p_action','p_user_text','p_confirmed']},
 {'name':'tm_set_payment_window_v18','args':['p_task_id','p_expected_updated_at','p_window','p_evidence','p_confirmed']}
])

def check_contract(db):
    rows = (db.table('tm_release_capabilities').select('version,capabilities')
            .eq('version',16).limit(1).execute().data or [])
    if len(rows) != 1:
        raise RuntimeError('V18 server release is missing; existing client must remain installed')
    caps = rows[0].get('capabilities') or {}
    expected = {'schema':16, 'client_protocol':PROTOCOL, 'native_flags':False,
                'payment_marker':'⚡️', 'payment_independent':True,
                'salary_runtime':'guarded_series_periods_v16', 'estimate_parser':'estimate16.1'}
    wrong = [key for key,value in expected.items() if caps.get(key) != value]
    if wrong:
        raise RuntimeError('V18 server capability mismatch: '+', '.join(wrong))
    result = db.rpc('tm_contract_v16', {'p_requirements':REQUIREMENTS}).execute().data
    if not isinstance(result,dict) or result.get('ok') is not True or result.get('checked') != len(REQUIREMENTS):
        names = [str(p.get('name','unknown')) for p in (result or {}).get('problems',[])] if isinstance(result,dict) else []
        raise RuntimeError('V18 RPC contract failed: '+', '.join(names[:20]))
    personal=db.rpc('tm_personal_contract_v18',{'p_instance_id':INSTANCE_ID}).execute().data
    if not isinstance(personal,dict) or personal.get('client')!=18 or personal.get('mode')!='personal_v16_upgrade':
        raise RuntimeError('V18 personal server contract required')
    return {'ok':True,'schema':18,'compatible_core':16,'protocol':PROTOCOL,'rpc_signatures':len(REQUIREMENTS),'display_profile_version':personal.get('review_profile_version')}


def probe_contexts(db, cfg, instance=INSTANCE_ID):
    """Exercise only read-only views/RPCs, including empty-queue response shapes."""
    from tm_workflows import active_project_groups
    review = db.rpc('tm_review_context_v18',{'p_instance_id':instance}).execute().data
    if not isinstance(review,dict) or not all(isinstance(review.get(k),list)
           for k in ('groups','tasks','proposals','workflows','estimates')):
        raise RuntimeError('V18 review context is incomplete')
    batch = db.rpc('tm_media_batch_v16',{'p_instance_id':instance,'p_limit':1}).execute().data
    if not isinstance(batch,dict) or not all(isinstance(batch.get(k),list) for k in ('attachments','projects')):
        raise RuntimeError('V18 media context is incomplete')
    salary_groups = 0
    for gid,g in active_project_groups(cfg,instance).items():
        context = db.rpc('tm_salary_context_v15', {'p_group_id':gid,'p_instance_id':instance,
                         'p_list_id':g['reminder_list_id']}).execute().data
        if not isinstance(context,dict) or not isinstance(context.get('periods'),list):
            raise RuntimeError('V18 salary context is incomplete')
        salary_groups += 1
    return {'read_only':True,'review_context':True,'media_context':True,'salary_groups':salary_groups}
