-- V19 phase 4. No UPDATE of existing records. Request behavior is opt-in per record.
CREATE FUNCTION tm_api_private.request_title(title text, state text)
RETURNS text LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT CASE state WHEN 'pending' THEN '➕ ' WHEN 'rejected' THEN 'Отклонено: ' ELSE '' END
 || regexp_replace(title,'^(➕ |Отклонено: )','');
$$;
CREATE FUNCTION tm_api_private.request_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE state text:=NEW.project_data->>'request_state';
BEGIN
 IF NEW.record_kind<>'project' OR state IS NULL THEN RETURN NEW; END IF;
 IF state NOT IN ('pending','confirmed','rejected') THEN RAISE EXCEPTION 'invalid_request_state'; END IF;
 IF coalesce((NEW.project_data->>'salary')::boolean,false) OR NEW.project_data->>'project_kind'='salary'
 OR EXISTS(SELECT 1 FROM public.tm_salary_periods_v15 WHERE task_id=NEW.id) THEN RAISE EXCEPTION 'salary_is_not_request'; END IF;
 IF state IN ('pending','rejected') THEN
  IF NEW.status='completed' OR NEW.completed_at IS NOT NULL OR NEW.project_data->>'work_status' IN ('in_progress','delivered') THEN RAISE EXCEPTION 'unconfirmed_request_cannot_complete'; END IF;
  -- waiting is a non-completed native-compatible state; the structured lifecycle
  -- is authoritative and separates rejection from active confirmed work.
  NEW.status:=CASE WHEN state='rejected' THEN 'waiting' ELSE 'open' END;
 END IF;
 NEW.title:=tm_api_private.request_title(NEW.title,state);
 RETURN NEW;
END $$;
CREATE TRIGGER tm_request_guard_v19 BEFORE INSERT OR UPDATE ON public.tasks
FOR EACH ROW EXECUTE FUNCTION tm_api_private.request_guard();

CREATE FUNCTION tm_api_private.request_effect(p_instance text,p_key text,p_operation text,p jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE t public.tasks%ROWTYPE; g public.telegram_chat_groups%ROWTYPE; pr public.tm_project_proposals%ROWTYPE;
 result jsonb; state text; previous text; e jsonb;
BEGIN
 IF p->'confirmed' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'explicit_confirmation_required'; END IF;
 IF p_operation='create_project_request' THEN
  SELECT * INTO STRICT pr FROM public.tm_project_proposals WHERE id=(p->>'proposal_id')::uuid FOR UPDATE;
  SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE id=pr.group_id AND reminder_list_instance_id=p_instance AND enabled AND monitoring_enabled AND rules_profile='projects_payments' FOR SHARE;
  IF pr.status<>'pending' OR pr.task_id IS NOT NULL OR pr.kind<>'create'
   OR pr.updated_at IS DISTINCT FROM (p->>'expected_proposal_updated_at')::timestamptz
   OR coalesce(pr.patch->>'work_status','planned')<>'planned'
   OR coalesce(pr.patch->>'payment_status','unknown')<>'unknown'
   OR EXISTS(SELECT 1 FROM jsonb_object_keys(pr.patch) k WHERE k NOT IN ('label','date_mmdd','date_iso','amount_rub','work_status','payment_status')) THEN RAISE EXCEPTION 'request_proposal_changed_or_invalid'; END IF;
  PERFORM tm_api_private.evidence(g.id,p->'evidence');
  PERFORM tm_api_private.evidence(g.id,pr.evidence);
  result:=public.tm_apply_project_proposal_v11(pr.id,NULL,true,false);
  SELECT * INTO STRICT t FROM public.tasks WHERE id=(result->>'task_id')::bigint FOR UPDATE;
  state:='pending';
 ELSIF p_operation='set_project_request_state' THEN
  SELECT * INTO STRICT t FROM public.tasks WHERE id=(p->>'project_id')::bigint FOR UPDATE;
  SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE id=t.context_group_id AND reminder_list_instance_id=p_instance AND enabled AND monitoring_enabled AND rules_profile='projects_payments' FOR SHARE;
  IF t.record_kind<>'project' OR t.project_archived_at IS NOT NULL OR t.updated_at IS DISTINCT FROM (p->>'expected_updated_at')::timestamptz THEN RAISE EXCEPTION 'project_changed_or_out_of_scope'; END IF;
  PERFORM tm_api_private.evidence(g.id,p->'evidence');
  state:=p->>'state'; previous:=t.project_data->>'request_state';
  IF state IS NULL OR NOT ((previous IS NULL AND state='pending') OR (previous='pending' AND state IN ('confirmed','rejected'))) THEN RAISE EXCEPTION 'request_transition_requires_review'; END IF;
  IF previous IS NULL AND (t.status<>'open' OR t.completed_at IS NOT NULL OR coalesce(t.project_data->>'work_status','planned')<>'planned' OR t.payment_status<>'unknown'
   OR EXISTS(SELECT 1 FROM public.tm_payment_allocations WHERE task_id=t.id)) THEN RAISE EXCEPTION 'existing_work_is_not_prospective'; END IF;
 ELSE RAISE EXCEPTION 'operation_not_allowed'; END IF;
 IF coalesce((t.project_data->>'salary')::boolean,false) OR t.project_data->>'project_kind'='salary'
  OR EXISTS(SELECT 1 FROM public.tm_salary_periods_v15 WHERE task_id=t.id) THEN RAISE EXCEPTION 'salary_is_not_request'; END IF;
 UPDATE public.tasks SET project_data=project_data||jsonb_build_object('request_state',state),
  status=CASE WHEN state='rejected' THEN 'waiting' ELSE 'open' END,
  cancelled_at=CASE WHEN state='rejected' THEN clock_timestamp() ELSE NULL END,
  reminder_flagged=NULL,updated_at=clock_timestamp() WHERE id=t.id;
 FOR e IN SELECT value FROM jsonb_array_elements(p->'evidence') WHERE value->>'kind'='telegram' LOOP
  INSERT INTO public.tm_project_links(task_id,chat_id,message_id) VALUES(t.id,(e->>'chat_id')::bigint,(e->>'message_id')::bigint) ON CONFLICT DO NOTHING;
 END LOOP;
 INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence)
 VALUES(t.id,'request_'||state,'api:'||p_instance,jsonb_build_object('request_state',previous,'title',t.title),
  (SELECT jsonb_build_object('request_state',state,'title',title,'status',status) FROM public.tasks WHERE id=t.id),p->'evidence');
 RETURN jsonb_build_object('applied',true,'task_id',t.id,'request_state',state,'native_sync','pending');
END $$;

-- Single read-only database snapshot: no pagination race or silent truncation.
CREATE FUNCTION public.tm_financial_snapshot_v19(p_instance_id text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE result jsonb; n bigint;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' THEN RAISE EXCEPTION 'invalid_scope'; END IF;
 SELECT count(*) INTO n FROM public.tasks t JOIN public.telegram_chat_groups g ON g.id=t.context_group_id
 WHERE t.record_kind='project' AND g.reminder_list_instance_id=p_instance_id;
 IF n>10000 THEN RAISE EXCEPTION 'financial_snapshot_requires_narrower_query'; END IF;
 SELECT jsonb_build_object('as_of',statement_timestamp(),'items',coalesce(jsonb_agg(jsonb_build_object(
  'id',t.id,'title',t.title,'context_group_id',t.context_group_id,'group_key',g.group_key,
  'status',t.status,'project_data',t.project_data,'payment_status',t.payment_status,'project_archived_at',t.project_archived_at,
  'completed_at',t.completed_at,'cancelled_at',t.cancelled_at,'updated_at',t.updated_at,
  'salary',EXISTS(SELECT 1 FROM public.tm_salary_periods_v15 s WHERE s.task_id=t.id),
  'ledger',jsonb_build_object('confirmed_component_sum',f.confirmed_component_sum::text,'received_net',f.received_net::text,'scope_complete',f.scope_complete),
  'workstreams',coalesce((SELECT jsonb_agg(jsonb_build_object('work_key',w.work_key,'work_type',w.work_type,'label',w.label,'amount',w.amount::text,'state',w.state)) FROM public.tm_workstreams w WHERE w.task_id=t.id AND w.state<>'retired'),'[]'::jsonb)
 ) ORDER BY t.id),'[]'::jsonb)) INTO result
 FROM public.tasks t JOIN public.telegram_chat_groups g ON g.id=t.context_group_id
 LEFT JOIN public.tm_project_finance_v14 f ON f.task_id=t.id
 WHERE t.record_kind='project' AND g.reminder_list_instance_id=p_instance_id;
 RETURN result;
END $$;
CREATE OR REPLACE FUNCTION public.tm_project_ack_v18(p_task_id bigint, p_expected_updated_at timestamp with time zone, p_instance_id text, p_list_id text, p_reminder_id text, p_state jsonb, p_project_data jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE t public.tasks%ROWTYPE; expected text; actual text; native_data jsonb:=p_project_data; native_label text; native_date text; req text; BEGIN SELECT * INTO STRICT t FROM public.tasks WHERE id=p_task_id FOR UPDATE;
IF NOT public.tm_project_scope_v16(t.id,p_instance_id,p_list_id) OR t.updated_at IS DISTINCT FROM p_expected_updated_at OR (t.apple_reminder_id IS NOT NULL AND lower(replace(t.apple_reminder_id,'x-apple-reminder://',''))<>lower(replace(p_reminder_id,'x-apple-reminder://',''))) THEN RAISE EXCEPTION 'project_changed_during_sync'; END IF;
IF jsonb_typeof(p_state->'completed') IS DISTINCT FROM 'boolean' OR p_state->'due_spec'->>'kind' IS DISTINCT FROM 'none' OR nullif(p_state->>'fingerprint','') IS NULL OR jsonb_typeof(p_project_data) IS DISTINCT FROM 'object' OR nullif(p_state->>'title','') IS NULL OR p_state ? 'flagged' THEN RAISE EXCEPTION 'incomplete_project_ack'; END IF;
IF coalesce(p_state->>'notes','')<>'' OR coalesce(t.description,'')<>'' OR octet_length(p_project_data::text)>200000 THEN RAISE EXCEPTION 'project_notes_disabled_or_oversized_data'; END IF;
IF p_project_data->'payment_window' IS DISTINCT FROM t.project_data->'payment_window' OR p_project_data->'payment_window_rule' IS DISTINCT FROM t.project_data->'payment_window_rule' THEN RAISE EXCEPTION 'native_cannot_change_payment_forecast'; END IF;
expected:=nullif(t.project_data->>'expected_payment_mmdd',''); actual:=p_state->>'title';
req:=t.project_data->>'request_state';
IF req IS NOT NULL THEN
 IF p_project_data->'request_state' IS DISTINCT FROM t.project_data->'request_state' THEN RAISE EXCEPTION 'native_cannot_change_request_state'; END IF;
 IF req IN ('pending','rejected') THEN
  IF p_state->'completed' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'unconfirmed_request_cannot_complete'; END IF;
  IF (req='pending' AND actual NOT LIKE '➕ %') OR (req='rejected' AND actual NOT LIKE 'Отклонено: %') THEN RAISE EXCEPTION 'request_marker_requires_review'; END IF;
  -- V18 parses an initial marker into label and loses the date. Correct only that
  -- legacy parsing artifact; retain native edits to label and amount.
  native_label:=native_data->>'label';
  native_label:=CASE WHEN req='pending' THEN substr(native_label,3) ELSE substr(native_label,12) END;
  native_date:=substring(native_label from '^([0-9]{2}\.[0-9]{2}) ');
  IF native_date IS NOT NULL THEN
   PERFORM make_date(2000,split_part(native_date,'.',1)::int,split_part(native_date,'.',2)::int);
   native_label:=substr(native_label,7);
  END IF;
  IF nullif(btrim(native_label),'') IS NULL THEN RAISE EXCEPTION 'request_label_required'; END IF;
  native_data:=native_data||jsonb_build_object('label',native_label,'date_mmdd',native_date,
   'date_iso',CASE WHEN native_date IS NOT DISTINCT FROM t.project_data->>'date_mmdd' THEN t.project_data->>'date_iso' ELSE NULL END,
   'work_status','planned');
 END IF;
END IF;

IF actual IS DISTINCT FROM public.tm_payment_title_v18(actual,t.payment_status,t.project_data,(now() AT TIME ZONE 'Asia/Yekaterinburg')::date) THEN RAISE EXCEPTION 'payment_suffix_inconsistent_with_server'; END IF;
UPDATE public.tasks SET title=actual,description=coalesce(p_state->>'notes',''),status=CASE WHEN (p_state->>'completed')::boolean THEN 'completed' ELSE 'open' END,completed_at=CASE WHEN (p_state->>'completed')::boolean THEN coalesce(completed_at,now()) ELSE NULL END,project_data=native_data||jsonb_build_object('expected_payment_mmdd',expected,'display_protocol','v18_payment_window','business_timezone','Asia/Yekaterinburg'),reminder_flagged=NULL,project_sync_snapshot=p_state,apple_reminder_id=p_reminder_id,apple_reminder_list_id=p_list_id,apple_reminder_list_name=(SELECT reminder_list_name FROM public.telegram_chat_groups WHERE id=t.context_group_id),reminder_last_seen_at=now(),apple_reminder_synced_at=now(),project_sync_error=NULL,sync_error=NULL,due_at=NULL,reminder_due_spec='{"kind":"none"}',updated_at=now() WHERE id=t.id;
INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state) VALUES(t.id,'sync_v18','mac:'||p_instance_id,jsonb_build_object('title',t.title,'payment_status',t.payment_status),(p_state-'notes')||jsonb_build_object('payment_status',t.payment_status)); RETURN jsonb_build_object('applied',true,'task_id',t.id); END $function$
;
CREATE OR REPLACE FUNCTION tm_api_private.effect(p_instance text, p_key text, p_operation text, p jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE t public.tasks%ROWTYPE; g public.telegram_chat_groups%ROWTYPE;
 pr public.tm_project_proposals%ROWTYPE; a jsonb; result jsonb; alloc jsonb:='[]';
BEGIN
 IF jsonb_typeof(p) IS DISTINCT FROM 'object' OR p->'confirmed' IS DISTINCT FROM 'true'::jsonb THEN
   RAISE EXCEPTION 'explicit_confirmation_required'; END IF;
 IF p_operation IN ('create_project_request','set_project_request_state') THEN
   RETURN tm_api_private.request_effect(p_instance,p_key,p_operation,p);
 END IF;
 IF p_operation='record_payment' THEN
   SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE group_key=p->>'group_key'
     AND reminder_list_instance_id=p_instance AND enabled AND monitoring_enabled AND rules_profile='projects_payments' FOR SHARE;
   IF jsonb_typeof(p->'allocations') IS DISTINCT FROM 'array' OR jsonb_array_length(p->'allocations') NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'allocations_required'; END IF;
   FOR a IN SELECT value FROM jsonb_array_elements(p->'allocations') ORDER BY (value->>'task_id')::bigint LOOP
     SELECT * INTO STRICT t FROM public.tasks WHERE id=(a->>'task_id')::bigint FOR UPDATE;
     IF t.context_group_id IS DISTINCT FROM g.id OR t.updated_at IS DISTINCT FROM (a->>'expected_updated_at')::timestamptz
        OR t.record_kind<>'project' OR t.project_archived_at IS NOT NULL THEN RAISE EXCEPTION 'project_changed_or_out_of_scope'; END IF;
     alloc:=alloc||jsonb_build_array(a-'expected_updated_at');
   END LOOP;
   PERFORM tm_api_private.evidence(g.id,p->'evidence');
   RETURN public.tm_record_payment_v14(p_key,g.group_key,p->'event',alloc,p->'evidence',true);
 END IF;
 IF p_operation='update_existing_project' THEN
   SELECT * INTO STRICT pr FROM public.tm_project_proposals WHERE id=(p->>'proposal_id')::uuid FOR UPDATE;
   IF pr.task_id IS NULL OR pr.task_id IS DISTINCT FROM (p->>'project_id')::bigint OR pr.status<>'pending'
     OR pr.updated_at IS DISTINCT FROM (p->>'expected_proposal_updated_at')::timestamptz THEN RAISE EXCEPTION 'proposal_changed'; END IF;
   -- Payment, salary, workflow and deletion have dedicated guarded operations.
   IF EXISTS(SELECT 1 FROM jsonb_object_keys(pr.patch) k WHERE k NOT IN ('label','date_mmdd','date_iso','amount_rub','amount_breakdown','amount_status','aliases'))
     THEN RAISE EXCEPTION 'proposal_requires_dedicated_action'; END IF;
 END IF;
 SELECT * INTO STRICT t FROM public.tasks WHERE id=(p->>'project_id')::bigint FOR UPDATE;
 SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE id=t.context_group_id AND enabled AND monitoring_enabled
   AND rules_profile='projects_payments' AND reminder_list_instance_id=p_instance FOR SHARE;
 IF t.record_kind<>'project' OR t.project_archived_at IS NOT NULL OR t.updated_at IS DISTINCT FROM (p->>'expected_updated_at')::timestamptz THEN RAISE EXCEPTION 'project_changed_or_out_of_scope'; END IF;
 PERFORM tm_api_private.evidence(g.id,p->'evidence');
 IF p_operation='set_payment_window' THEN
   RETURN public.tm_set_payment_window_v18(t.id,t.updated_at,p->'window',p->'evidence',true);
 ELSIF p_operation='update_existing_project' THEN
   IF pr.group_id IS DISTINCT FROM g.id OR EXISTS(SELECT 1 FROM public.tm_salary_periods_v15 WHERE task_id=t.id)
      OR coalesce(t.project_data->>'project_kind','')='salary' THEN RAISE EXCEPTION 'proposal_scope_or_salary_policy_required'; END IF;
   PERFORM tm_api_private.evidence(g.id,pr.evidence);
   result:=public.tm_apply_project_proposal_v11(pr.id,t.updated_at,true,false);
   UPDATE public.tasks SET title=public.tm_payment_title_v18(title,payment_status,project_data,(now() AT TIME ZONE 'Asia/Yekaterinburg')::date),reminder_flagged=NULL WHERE id=t.id;
   RETURN result;
 ELSE RAISE EXCEPTION 'operation_not_allowed'; END IF;
END $$;

CREATE OR REPLACE FUNCTION public.tm_api_execute_v19(p_instance_id text, p_request_key text, p_operation text, p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE saved tm_api_private.operations%ROWTYPE; digest bytea; result jsonb;
 f public.tm_review_focus_state_v18%ROWTYPE; r public.tm_review_items%ROWTYPE;
 effect_result jsonb; effect_name text; business_key text;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' OR NOT public.tm_instance_known_v18(p_instance_id)
   OR length(coalesce(p_request_key,'')) NOT BETWEEN 16 AND 128 OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
   OR p_operation IS NULL OR p_operation NOT IN ('set_review_focus','answer_review_question','set_payment_window','record_payment','update_existing_project','create_project_request','set_project_request_state')
   THEN RAISE EXCEPTION 'invalid_operation_scope'; END IF;
 digest:=sha256(convert_to(p_payload::text,'UTF8'));
 PERFORM pg_advisory_xact_lock(hashtextextended('tm_api_v19:'||p_instance_id||':'||p_request_key,0));
 SELECT * INTO saved FROM tm_api_private.operations WHERE instance_id=p_instance_id AND request_key=p_request_key;
 IF FOUND THEN
   IF saved.operation IS DISTINCT FROM p_operation OR saved.payload_hash IS DISTINCT FROM digest THEN RAISE EXCEPTION 'idempotency_key_collision'; END IF;
   RETURN saved.result;
 END IF;
 business_key:='v19:'||encode(sha256(convert_to(p_instance_id||':'||p_request_key,'UTF8')),'hex');
 IF p_operation='set_review_focus' THEN
   IF p_payload->>'review_id' IS NULL OR p_payload->>'review_revision' IS NULL OR p_payload->>'expected_version' IS NULL THEN RAISE EXCEPTION 'review_identity_required'; END IF;
   result:=public.tm_review_focus_set_v18(p_instance_id,'personal-review',(p_payload->>'review_id')::uuid,
      (p_payload->>'review_revision')::integer,(p_payload->>'expected_version')::integer);
 ELSIF p_operation='answer_review_question' THEN
   IF p_payload->'confirmed' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'explicit_confirmation_required'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended('review_focus:'||p_instance_id||':personal-review',0));
   SELECT * INTO STRICT f FROM public.tm_review_focus_state_v18 WHERE instance_id=p_instance_id AND scope_key='personal-review' FOR UPDATE;
   IF f.version IS DISTINCT FROM (p_payload->>'expected_version')::integer OR f.review_id IS NULL
      OR f.review_id IS DISTINCT FROM (p_payload->>'review_id')::uuid OR f.review_revision IS DISTINCT FROM (p_payload->>'review_revision')::integer THEN RAISE EXCEPTION 'review_focus_changed'; END IF;
   SELECT * INTO STRICT r FROM public.tm_review_items WHERE id=f.review_id FOR UPDATE;
   IF r.revision IS DISTINCT FROM f.review_revision OR r.status NOT IN ('open','awaiting_user')
     OR (r.group_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.telegram_chat_groups WHERE id=r.group_id AND enabled AND monitoring_enabled AND reminder_list_instance_id=p_instance_id))
     OR (r.group_id IS NULL AND r.reference->>'instance_id' IS DISTINCT FROM p_instance_id) THEN RAISE EXCEPTION 'review_changed_or_out_of_scope'; END IF;
   IF jsonb_array_length(r.evidence)>0 THEN PERFORM tm_api_private.evidence(r.group_id,r.evidence); END IF;
   IF p_payload->>'action'='resolved' THEN
     effect_name:=p_payload->'effect'->>'operation';
     IF effect_name IS NULL OR effect_name NOT IN ('set_payment_window','update_existing_project','set_project_request_state') OR r.task_id IS NULL
       OR r.task_id IS DISTINCT FROM (p_payload->'effect'->'arguments'->>'project_id')::bigint THEN RAISE EXCEPTION 'linked_business_action_required'; END IF;
     effect_result:=tm_api_private.effect(p_instance_id,business_key,effect_name,p_payload->'effect'->'arguments');
   ELSIF p_payload->'effect' IS NOT NULL AND p_payload->'effect'<>'null'::jsonb THEN RAISE EXCEPTION 'unexpected_business_action'; END IF;
   result:=public.tm_review_focus_reply_v18(p_instance_id,'personal-review',f.version,business_key,
       p_payload->>'action',p_payload->>'user_text',true);
   IF effect_result IS NOT NULL THEN result:=result||jsonb_build_object('business_result',effect_result,'requires_separate_apply',false); END IF;
 ELSE
   result:=tm_api_private.effect(p_instance_id,business_key,p_operation,p_payload);
 END IF;
 INSERT INTO tm_api_private.operations(instance_id,request_key,operation,payload_hash,result)
 VALUES(p_instance_id,p_request_key,p_operation,digest,result);
 RETURN result;
END $$;

REVOKE ALL ON FUNCTION tm_api_private.request_title(text,text),tm_api_private.request_guard(),tm_api_private.request_effect(text,text,text,jsonb),public.tm_financial_snapshot_v19(text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION tm_api_private.request_title(text,text),tm_api_private.request_guard(),tm_api_private.request_effect(text,text,text,jsonb),public.tm_financial_snapshot_v19(text) TO service_role;
NOTIFY pgrst,'reload schema';
