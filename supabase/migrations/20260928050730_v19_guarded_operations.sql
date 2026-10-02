-- Additive V19 gateway. Existing V18 functions and project rows remain unchanged.
CREATE SCHEMA tm_api_private;
REVOKE ALL ON SCHEMA tm_api_private FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA tm_api_private TO service_role;
CREATE TABLE tm_api_private.operations (
  instance_id text NOT NULL,
  request_key text NOT NULL CHECK (length(request_key) BETWEEN 16 AND 128),
  operation text NOT NULL,
  payload_hash bytea NOT NULL,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(instance_id, request_key)
);
ALTER TABLE tm_api_private.operations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON tm_api_private.operations FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON tm_api_private.operations TO service_role;
CREATE TRIGGER operations_append_only BEFORE UPDATE OR DELETE ON tm_api_private.operations
FOR EACH ROW EXECUTE FUNCTION public.tm_append_only_v14();

CREATE FUNCTION tm_api_private.evidence(p_group_id bigint, p_evidence jsonb)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE e jsonb;
BEGIN
  IF jsonb_typeof(p_evidence) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'evidence_required'; END IF;
  -- Prevent an edit/delete from racing the token validation and the business change.
  FOR e IN SELECT value FROM jsonb_array_elements(p_evidence)
           WHERE coalesce(value->>'kind','telegram')='telegram'
           ORDER BY (value->>'chat_id')::bigint,(value->>'message_id')::bigint LOOP
    PERFORM 1 FROM public.telegram_messages WHERE chat_id=(e->>'chat_id')::bigint
      AND message_id=(e->>'message_id')::bigint FOR SHARE;
    PERFORM 1 FROM public.telegram_attachments WHERE chat_id=(e->>'chat_id')::bigint
      AND message_id=(e->>'message_id')::bigint FOR SHARE;
  END LOOP;
  IF NOT public.tm_evidence_valid_v14(p_group_id,p_evidence) THEN RAISE EXCEPTION 'evidence_changed_or_missing'; END IF;
END $$;

CREATE FUNCTION tm_api_private.effect(p_instance text, p_key text, p_operation text, p jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE t public.tasks%ROWTYPE; g public.telegram_chat_groups%ROWTYPE;
 pr public.tm_project_proposals%ROWTYPE; a jsonb; result jsonb; alloc jsonb:='[]';
BEGIN
 IF jsonb_typeof(p) IS DISTINCT FROM 'object' OR p->'confirmed' IS DISTINCT FROM 'true'::jsonb THEN
   RAISE EXCEPTION 'explicit_confirmation_required'; END IF;
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

CREATE FUNCTION public.tm_api_execute_v19(p_instance_id text, p_request_key text, p_operation text, p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE saved tm_api_private.operations%ROWTYPE; digest bytea; result jsonb;
 f public.tm_review_focus_state_v18%ROWTYPE; r public.tm_review_items%ROWTYPE;
 effect_result jsonb; effect_name text; business_key text;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' OR NOT public.tm_instance_known_v18(p_instance_id)
   OR length(coalesce(p_request_key,'')) NOT BETWEEN 16 AND 128 OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
   OR p_operation IS NULL OR p_operation NOT IN ('set_review_focus','answer_review_question','set_payment_window','record_payment','update_existing_project')
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
     IF effect_name IS NULL OR effect_name NOT IN ('set_payment_window','update_existing_project') OR r.task_id IS NULL
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
REVOKE ALL ON FUNCTION tm_api_private.evidence(bigint,jsonb),tm_api_private.effect(text,text,text,jsonb),public.tm_api_execute_v19(text,text,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION tm_api_private.evidence(bigint,jsonb),tm_api_private.effect(text,text,text,jsonb),public.tm_api_execute_v19(text,text,text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
