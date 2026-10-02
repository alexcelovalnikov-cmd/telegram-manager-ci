BEGIN;
-- Restore V18 ack only before any request rows exist. Otherwise disable API writes
-- and retain request-aware sync; rolling back those rows would erase user state.
DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.tasks WHERE project_data ? 'request_state')
THEN RAISE EXCEPTION 'Request records exist: keep compatibility sync, disable gateway writes only'; END IF; END $$;
REVOKE EXECUTE ON FUNCTION tm_api_private.request_effect(text,text,text,jsonb) FROM service_role;
CREATE OR REPLACE FUNCTION public.tm_project_ack_v18(p_task_id bigint, p_expected_updated_at timestamp with time zone, p_instance_id text, p_list_id text, p_reminder_id text, p_state jsonb, p_project_data jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE t public.tasks%ROWTYPE; expected text; actual text; BEGIN SELECT * INTO STRICT t FROM public.tasks WHERE id=p_task_id FOR UPDATE;
IF NOT public.tm_project_scope_v16(t.id,p_instance_id,p_list_id) OR t.updated_at IS DISTINCT FROM p_expected_updated_at OR (t.apple_reminder_id IS NOT NULL AND lower(replace(t.apple_reminder_id,'x-apple-reminder://',''))<>lower(replace(p_reminder_id,'x-apple-reminder://',''))) THEN RAISE EXCEPTION 'project_changed_during_sync'; END IF;
IF jsonb_typeof(p_state->'completed') IS DISTINCT FROM 'boolean' OR p_state->'due_spec'->>'kind' IS DISTINCT FROM 'none' OR nullif(p_state->>'fingerprint','') IS NULL OR jsonb_typeof(p_project_data) IS DISTINCT FROM 'object' OR nullif(p_state->>'title','') IS NULL OR p_state ? 'flagged' THEN RAISE EXCEPTION 'incomplete_project_ack'; END IF;
IF coalesce(p_state->>'notes','')<>'' OR coalesce(t.description,'')<>'' OR octet_length(p_project_data::text)>200000 THEN RAISE EXCEPTION 'project_notes_disabled_or_oversized_data'; END IF;
IF p_project_data->'payment_window' IS DISTINCT FROM t.project_data->'payment_window' OR p_project_data->'payment_window_rule' IS DISTINCT FROM t.project_data->'payment_window_rule' THEN RAISE EXCEPTION 'native_cannot_change_payment_forecast'; END IF;
expected:=nullif(t.project_data->>'expected_payment_mmdd',''); actual:=p_state->>'title';
IF actual IS DISTINCT FROM public.tm_payment_title_v18(actual,t.payment_status,t.project_data,(now() AT TIME ZONE 'Asia/Yekaterinburg')::date) THEN RAISE EXCEPTION 'payment_suffix_inconsistent_with_server'; END IF;
UPDATE public.tasks SET title=actual,description=coalesce(p_state->>'notes',''),status=CASE WHEN (p_state->>'completed')::boolean THEN 'completed' ELSE 'open' END,completed_at=CASE WHEN (p_state->>'completed')::boolean THEN coalesce(completed_at,now()) ELSE NULL END,project_data=p_project_data||jsonb_build_object('expected_payment_mmdd',expected,'display_protocol','v18_payment_window','business_timezone','Asia/Yekaterinburg'),reminder_flagged=NULL,project_sync_snapshot=p_state,apple_reminder_id=p_reminder_id,apple_reminder_list_id=p_list_id,apple_reminder_list_name=(SELECT reminder_list_name FROM public.telegram_chat_groups WHERE id=t.context_group_id),reminder_last_seen_at=now(),apple_reminder_synced_at=now(),project_sync_error=NULL,sync_error=NULL,due_at=NULL,reminder_due_spec='{"kind":"none"}',updated_at=now() WHERE id=t.id;
INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state) VALUES(t.id,'sync_v18','mac:'||p_instance_id,jsonb_build_object('title',t.title,'payment_status',t.payment_status),(p_state-'notes')||jsonb_build_object('payment_status',t.payment_status)); RETURN jsonb_build_object('applied',true,'task_id',t.id); END $function$
;

COMMIT;
