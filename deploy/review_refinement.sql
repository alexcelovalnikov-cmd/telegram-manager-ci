BEGIN;
CREATE OR REPLACE FUNCTION public.tm_apply_project_proposal_v11(p_proposal_id uuid, p_expected_task_updated_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_user_confirmed boolean DEFAULT false, p_remove_paid_reminder boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE pr public.tm_project_proposals%ROWTYPE; g public.telegram_chat_groups%ROWTYPE; t public.tasks%ROWTYPE; e jsonb; data jsonb; rendered_title text; work text; pay text; v_result jsonb; peers integer;
BEGIN
 SELECT * INTO STRICT pr FROM public.tm_project_proposals WHERE id=p_proposal_id FOR UPDATE;
 IF pr.status='applied' THEN RETURN pr.result; END IF;
 IF pr.status<>'pending' THEN RAISE EXCEPTION 'Proposal not pending'; END IF;
 SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE id=pr.group_id;
 IF NOT g.enabled OR NOT g.monitoring_enabled OR g.rules_profile<>'projects_payments' OR NOT public.tm_chat_allowed_v10(pr.chat_id) THEN RAISE EXCEPTION 'Project monitoring paused'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('project_import:'||g.reminder_list_instance_id||':'||g.reminder_list_id,0));
 FOR e IN SELECT value FROM jsonb_array_elements(pr.evidence) LOOP
  IF e->>'content_token' IS DISTINCT FROM public.tm_content_token_v11(coalesce((e->>'chat_id')::bigint,pr.chat_id),(e->>'message_id')::bigint) OR NOT EXISTS(SELECT 1 FROM public.telegram_messages WHERE chat_id=coalesce((e->>'chat_id')::bigint,pr.chat_id) AND message_id=(e->>'message_id')::bigint AND NOT is_deleted) THEN RAISE EXCEPTION 'Evidence edited/deleted; review again'; END IF;
 END LOOP;
 IF pr.history_only AND p_user_confirmed IS DISTINCT FROM true THEN RAISE EXCEPTION 'Historical proposal requires review'; END IF;
 IF pr.kind='payment' OR pr.patch->>'payment_status'='paid' OR p_remove_paid_reminder THEN
  IF p_user_confirmed IS DISTINCT FROM true THEN RAISE EXCEPTION 'Actual payment/removal requires explicit user confirmation'; END IF;
 END IF;
 IF p_user_confirmed IS DISTINCT FROM true AND pr.confidence<0.95 THEN RAISE EXCEPTION 'Ambiguous proposal requires user review'; END IF;
 IF pr.task_id IS NULL THEN
  IF pr.kind<>'create' OR pr.patch->>'payment_status'='paid' OR p_remove_paid_reminder THEN RAISE EXCEPTION 'Cannot pay/remove an unidentified project'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.tm_project_import_state s WHERE s.group_id=g.id AND s.instance_id=g.reminder_list_instance_id AND s.list_id=g.reminder_list_id AND s.status='complete' AND s.last_snapshot_at>now()-interval '5 minutes') THEN RAISE EXCEPTION 'Fresh completed import required before creating projects'; END IF;
  data:=pr.patch-'payment_status'; rendered_title:=public.tm_project_title_v11(data);
  SELECT count(*) INTO peers FROM public.tasks WHERE context_group_id=g.id AND record_kind='project' AND project_archived_at IS NULL AND lower(btrim(project_data->>'label'))=lower(btrim(data->>'label'));
  IF peers>0 OR jsonb_array_length(pr.match_candidates)>0 THEN RAISE EXCEPTION 'Possible existing project: link it instead of creating a duplicate'; END IF;
  work:=coalesce(data->>'work_status','planned'); pay:=coalesce(pr.patch->>'payment_status','unknown');
  INSERT INTO public.tasks(title,description,status,target_app,context_group_id,record_kind,project_data,payment_status,reminder_flagged,reminder_due_spec,source_chat_id,source_message_id,confidence)
  VALUES(rendered_title,'',CASE WHEN work='delivered' THEN 'completed' ELSE 'open' END,'reminders',g.id,'project',data,pay,pay='awaiting','{"kind":"none"}',pr.chat_id,(pr.evidence->0->>'message_id')::bigint,pr.confidence) RETURNING * INTO t;
 ELSE
  SELECT * INTO STRICT t FROM public.tasks WHERE id=pr.task_id FOR UPDATE;
  IF t.updated_at IS DISTINCT FROM p_expected_task_updated_at OR t.record_kind<>'project' OR t.context_group_id<>g.id OR t.project_archived_at IS NOT NULL THEN RAISE EXCEPTION 'Project changed since review'; END IF;
  data:=(CASE WHEN pr.patch ? 'amount_rub' AND NOT pr.patch ? 'amount_breakdown' THEN t.project_data-'amount_breakdown' ELSE t.project_data END)||(pr.patch-'payment_status'); rendered_title:=CASE WHEN pr.patch ?| ARRAY['label','date_mmdd','date_iso','amount_rub','amount_breakdown'] THEN public.tm_project_title_v11(data) ELSE t.title END;
  work:=coalesce(data->>'work_status',CASE WHEN t.status='completed' THEN 'delivered' ELSE 'planned' END); pay:=coalesce(pr.patch->>'payment_status',t.payment_status);
  IF t.payment_status='paid' AND pay<>'paid' THEN RAISE EXCEPTION 'Reversing a confirmed payment requires separate reconciliation'; END IF;
  IF p_remove_paid_reminder AND pay<>'paid' THEN RAISE EXCEPTION 'Removal only after actual payment'; END IF;
  UPDATE public.tasks SET title=rendered_title,project_data=data,status=CASE WHEN work='delivered' THEN 'completed' ELSE 'open' END,completed_at=CASE WHEN work='delivered' THEN coalesce(completed_at,now()) ELSE NULL END,payment_status=pay,reminder_flagged=CASE WHEN pay='awaiting' THEN true ELSE reminder_flagged END,payment_confirmed_at=CASE WHEN pay='paid' THEN coalesce(payment_confirmed_at,now()) ELSE payment_confirmed_at END,payment_evidence=CASE WHEN pay='paid' THEN jsonb_build_object('user_confirmed',p_user_confirmed,'chat_id',pr.chat_id,'messages',pr.evidence,'proposal_id',pr.id) ELSE payment_evidence END,project_delete_request=CASE WHEN p_remove_paid_reminder THEN jsonb_build_object('approved',true,'state','approved','proposal_id',pr.id,'approved_at',now()) ELSE project_delete_request END,updated_at=now() WHERE id=t.id;
 END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(pr.evidence) LOOP INSERT INTO public.tm_project_links(task_id,chat_id,message_id) VALUES(t.id,coalesce((e->>'chat_id')::bigint,pr.chat_id),(e->>'message_id')::bigint) ON CONFLICT DO NOTHING; END LOOP;
 v_result:=jsonb_build_object('applied',true,'task_id',t.id,'native_sync','pending','payment_confirmed',coalesce(pr.patch->>'payment_status','')='paid','removal_requested',p_remove_paid_reminder);
 UPDATE public.tm_project_proposals SET status='applied',task_id=t.id,result=v_result,updated_at=now() WHERE id=pr.id;
 INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence) VALUES(t.id,pr.kind,CASE WHEN p_user_confirmed THEN 'user_confirmed' ELSE 'reviewed_context' END,jsonb_build_object('title',t.title,'payment_status',t.payment_status),pr.patch,pr.evidence);
 RETURN v_result;
END $function$
;
CREATE OR REPLACE FUNCTION public.tm_project_title_v11(p_data jsonb)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO ''
AS $function$
DECLARE label text:=btrim(p_data->>'label'); d text:=p_data->>'date_mmdd'; n numeric; amt text; m integer; day integer;
BEGIN
IF label IS NULL OR label='' OR length(label)>350 OR label ~ '[[:cntrl:]]' THEN RAISE EXCEPTION 'Invalid project label'; END IF;
IF d IS NOT NULL THEN
 IF d !~ '^[0-9]{2}\.[0-9]{2}$' THEN RAISE EXCEPTION 'Invalid month.day'; END IF;
 m:=split_part(d,'.',1)::int; day:=split_part(d,'.',2)::int; PERFORM make_date(2000,m,day); label:=d||' '||label;
END IF;
IF p_data->>'date_iso' IS NOT NULL AND (p_data->>'date_iso')::date IS NOT NULL AND to_char((p_data->>'date_iso')::date,'MM.DD') IS DISTINCT FROM d THEN RAISE EXCEPTION 'Date and title prefix disagree'; END IF;
IF coalesce(p_data->'amount_breakdown'->>'raw','')<>'' AND ((p_data->'amount_breakdown'->'complete'='true'::jsonb AND (p_data->'amount_breakdown'->>'total')::numeric=(p_data->>'amount_rub')::numeric) OR (p_data->'amount_breakdown'->'complete'='false'::jsonb AND p_data->>'amount_rub' IS NULL)) THEN
 IF length(p_data->'amount_breakdown'->>'raw')>500 OR (p_data->'amount_breakdown'->>'raw') ~ '[[:cntrl:]]' THEN RAISE EXCEPTION 'Invalid amount expression'; END IF;
 RETURN label||' - '||(p_data->'amount_breakdown'->>'raw');
END IF;
IF p_data->>'amount_rub' IS NOT NULL THEN
 n:=(p_data->>'amount_rub')::numeric; IF n<0 OR n>1000000000 OR round(n,2)<>n OR n::text IN ('NaN','Infinity','-Infinity') THEN RAISE EXCEPTION 'Invalid project amount'; END IF;
 amt:=CASE WHEN n>=1000 THEN (n/1000)::text ELSE n::text END;
 IF position('.' in amt)>0 THEN amt:=rtrim(rtrim(amt,'0'),'.'); END IF;
 label:=label||' - '||replace(amt,'.',',')||CASE WHEN n>=1000 THEN 'к' ELSE '₽' END;
END IF;
RETURN label; END $function$
;
CREATE OR REPLACE FUNCTION public.tm_propose_project_v11(p_request_key text, p_group_key text, p_chat_id bigint, p_task_id bigint, p_kind text, p_patch jsonb, p_evidence jsonb, p_matches jsonb DEFAULT '[]'::jsonb, p_confidence numeric DEFAULT 0.5, p_history_only boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE gid bigint; e jsonb; pr public.tm_project_proposals%ROWTYPE;
BEGIN
 SELECT g.id INTO gid FROM public.telegram_chat_groups g JOIN public.telegram_chat_group_members m ON m.group_id=g.id WHERE g.group_key=p_group_key AND g.enabled AND g.monitoring_enabled AND g.rules_profile='projects_payments' AND m.enabled AND m.chat_id=p_chat_id AND public.tm_chat_allowed_v10(p_chat_id);
 IF gid IS NULL THEN RAISE EXCEPTION 'Project chat/group not authorized'; END IF;
 IF p_kind NOT IN ('create','update','payment') OR jsonb_typeof(p_patch) IS DISTINCT FROM 'object' OR jsonb_typeof(p_evidence) IS DISTINCT FROM 'array' OR jsonb_array_length(p_evidence) NOT BETWEEN 1 AND 20 OR length(p_request_key) NOT BETWEEN 8 AND 200 OR octet_length(p_patch::text)>20000 THEN RAISE EXCEPTION 'Invalid proposal'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('label','date_mmdd','date_iso','amount_rub','amount_breakdown','amount_status','aliases','work_status','payment_status')) THEN RAISE EXCEPTION 'Unexpected project patch field'; END IF;
 IF p_patch->>'work_status' IS NOT NULL AND p_patch->>'work_status' NOT IN ('planned','in_progress','delivered') THEN RAISE EXCEPTION 'Invalid work state'; END IF;
 IF p_patch->>'payment_status' IS NOT NULL AND p_patch->>'payment_status' NOT IN ('unknown','awaiting','partial','paid') THEN RAISE EXCEPTION 'Invalid payment state'; END IF;
 IF p_task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.tasks WHERE id=p_task_id AND context_group_id=gid AND record_kind='project' AND project_archived_at IS NULL) THEN RAISE EXCEPTION 'Project not in this group'; END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(p_evidence) LOOP
  IF NOT public.tm_group_chat_allowed_v14(gid,coalesce((e->>'chat_id')::bigint,p_chat_id)) THEN RAISE EXCEPTION 'Evidence out of group'; END IF;
  IF e->>'content_token' IS NULL OR e->>'content_token' IS DISTINCT FROM public.tm_content_token_v11(coalesce((e->>'chat_id')::bigint,p_chat_id),(e->>'message_id')::bigint) OR NOT EXISTS(SELECT 1 FROM public.telegram_messages WHERE chat_id=coalesce((e->>'chat_id')::bigint,p_chat_id) AND message_id=(e->>'message_id')::bigint AND NOT is_deleted) THEN RAISE EXCEPTION 'Proposal evidence is not current'; END IF;
 END LOOP;
 INSERT INTO public.tm_project_proposals(request_key,group_id,chat_id,task_id,kind,patch,evidence,match_candidates,confidence,history_only) VALUES(p_request_key,gid,p_chat_id,p_task_id,p_kind,p_patch,p_evidence,coalesce(p_matches,'[]'),greatest(0,least(1,p_confidence)),p_history_only) ON CONFLICT(request_key) DO NOTHING;
 SELECT * INTO STRICT pr FROM public.tm_project_proposals WHERE request_key=p_request_key;
 IF pr.group_id<>gid OR pr.chat_id<>p_chat_id OR pr.task_id IS DISTINCT FROM p_task_id OR pr.kind<>p_kind OR pr.patch<>p_patch OR pr.evidence<>p_evidence THEN RAISE EXCEPTION 'Proposal key collision'; END IF;
 RETURN to_jsonb(pr);
END $function$
;

CREATE TABLE IF NOT EXISTS tm_api_private.review_proposals_v22 (
 proposal_id uuid PRIMARY KEY REFERENCES public.tm_project_proposals(id),
 instance_id text NOT NULL, review_id uuid NOT NULL REFERENCES public.tm_review_items(id),
 review_revision integer NOT NULL, focus_version integer NOT NULL,
 project_id bigint NOT NULL REFERENCES public.tasks(id), expected_updated_at timestamptz NOT NULL,
 proposal_updated_at timestamptz NOT NULL, before_title text NOT NULL, after_title text NOT NULL,
 patch jsonb NOT NULL, evidence jsonb NOT NULL, rationale text NOT NULL,
 fingerprint text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON tm_api_private.review_proposals_v22 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON tm_api_private.review_proposals_v22 TO service_role;
ALTER TABLE tm_api_private.review_proposals_v22 ENABLE ROW LEVEL SECURITY;
DROP TRIGGER IF EXISTS review_proposals_append_only ON tm_api_private.review_proposals_v22;
CREATE TRIGGER review_proposals_append_only BEFORE UPDATE OR DELETE ON tm_api_private.review_proposals_v22 FOR EACH ROW EXECUTE FUNCTION public.tm_append_only_v14();

CREATE OR REPLACE FUNCTION tm_api_private.validate_components_v22(p jsonb)
RETURNS void LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE b jsonb:=p->'amount_breakdown'; term jsonb; m text[]; n numeric; total numeric:=0; rendered text:='';
BEGIN
 IF b IS NULL THEN RETURN; END IF;
 IF b->'complete' IS DISTINCT FROM 'true'::jsonb OR jsonb_typeof(b->'terms') IS DISTINCT FROM 'array' OR jsonb_array_length(b->'terms') NOT BETWEEN 1 AND 16 THEN RAISE EXCEPTION 'components_incomplete'; END IF;
 FOR term IN SELECT value FROM jsonb_array_elements(b->'terms') LOOP
  m:=regexp_match(btrim(term->>'raw'),'^(?:([^?+[:cntrl:]]{1,80})[[:space:]]+)?([0-9]+(?:[.,][0-9]{1,3})?)[[:space:]]*(к|k|₽|руб[.]?|р[.]?)$','i');
  IF m IS NULL OR term->>'state' IS DISTINCT FROM 'stated' THEN RAISE EXCEPTION 'components_ambiguous'; END IF;
  n:=replace(m[2],',','.')::numeric * CASE WHEN lower(m[3]) IN ('к','k') THEN 1000 ELSE 1 END;
  IF n<0 OR n>1000000000 OR round(n,2)<>n OR n IS DISTINCT FROM (term->>'amount')::numeric OR nullif(btrim(m[1]),'') IS DISTINCT FROM nullif(term->>'label','') THEN RAISE EXCEPTION 'components_inconsistent'; END IF;
  total:=total+n; rendered:=rendered||CASE WHEN rendered='' THEN '' ELSE ' + ' END||btrim(term->>'raw');
 END LOOP;
 IF total>1000000000 OR total IS DISTINCT FROM (p->>'amount_rub')::numeric OR total IS DISTINCT FROM (b->>'total')::numeric OR total IS DISTINCT FROM (b->>'known_subtotal')::numeric
 OR rendered IS DISTINCT FROM regexp_replace(btrim(b->>'raw'),'[[:space:]]*[+][[:space:]]*',' + ','g') THEN RAISE EXCEPTION 'components_inconsistent'; END IF;
END $$;
REVOKE ALL ON FUNCTION tm_api_private.validate_components_v22(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION tm_api_private.validate_components_v22(jsonb) TO service_role;

CREATE OR REPLACE FUNCTION tm_api_private.prepare_review_v22(p_instance text,p jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path='' AS $$
DECLARE f public.tm_review_focus_state_v18; r public.tm_review_items; t public.tasks; g public.telegram_chat_groups;
 patch jsonb:=p->'patch'; e jsonb; pr jsonb; after_title text; stamp text; stored tm_api_private.review_proposals_v22;
BEGIN
 IF p->>'evidence_assessment' IS DISTINCT FROM 'supported' THEN RAISE EXCEPTION 'evidence_ambiguous'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('review_focus:'||p_instance||':personal-review',0));
 SELECT * INTO STRICT f FROM public.tm_review_focus_state_v18 WHERE instance_id=p_instance AND scope_key='personal-review' FOR UPDATE;
 IF f.review_id IS NULL OR f.review_id IS DISTINCT FROM (p->>'review_id')::uuid OR f.review_revision IS DISTINCT FROM (p->>'review_revision')::integer OR f.version IS DISTINCT FROM (p->>'expected_version')::integer THEN RAISE EXCEPTION 'review_focus_changed'; END IF;
 SELECT * INTO STRICT r FROM public.tm_review_items WHERE id=f.review_id FOR UPDATE;
 IF r.revision<>f.review_revision OR r.status NOT IN ('open','awaiting_user') OR r.task_id IS DISTINCT FROM (p->>'project_id')::bigint THEN RAISE EXCEPTION 'review_changed_or_out_of_scope'; END IF;
 SELECT * INTO STRICT t FROM public.tasks WHERE id=r.task_id FOR UPDATE;
 SELECT * INTO STRICT g FROM public.telegram_chat_groups WHERE id=t.context_group_id AND enabled AND monitoring_enabled AND rules_profile='projects_payments' AND reminder_list_instance_id=p_instance FOR SHARE;
 IF r.group_id IS DISTINCT FROM g.id OR t.updated_at IS DISTINCT FROM (p->>'expected_updated_at')::timestamptz OR t.record_kind<>'project' OR t.project_archived_at IS NOT NULL OR t.status='cancelled'
 OR coalesce(t.project_data->>'request_state','') IN ('pending','rejected') OR coalesce(t.project_data->>'project_kind','')='salary' OR EXISTS(SELECT 1 FROM public.tm_salary_periods_v15 WHERE task_id=t.id) THEN RAISE EXCEPTION 'project_changed_or_out_of_scope'; END IF;
 IF jsonb_typeof(patch) IS DISTINCT FROM 'object' OR patch='{}' OR EXISTS(SELECT 1 FROM jsonb_object_keys(patch) k WHERE k NOT IN ('label','date_mmdd','date_iso','amount_rub','amount_breakdown','amount_status','aliases')) THEN RAISE EXCEPTION 'proposal_requires_dedicated_action'; END IF;
 PERFORM tm_api_private.validate_components_v22(patch);
 IF length(btrim(coalesce(p->>'rationale','')))<2 THEN RAISE EXCEPTION 'rationale_required'; END IF;
 PERFORM tm_api_private.evidence(g.id,p->'evidence');
 IF jsonb_array_length(r.evidence)>0 THEN PERFORM tm_api_private.evidence(g.id,r.evidence); END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(p->'evidence') LOOP
  IF e->>'kind' IS DISTINCT FROM 'telegram' OR NOT EXISTS(SELECT 1 FROM public.telegram_messages m WHERE m.chat_id=(e->>'chat_id')::bigint AND m.message_id=(e->>'message_id')::bigint AND m.deleted_at IS NULL AND NOT coalesce(m.is_deleted,false)) OR NOT EXISTS(SELECT 1 FROM tm_api_private.project_chats_v21(p_instance,t.id) c WHERE c.chat_id=(e->>'chat_id')::bigint) THEN RAISE EXCEPTION 'linked_telegram_evidence_required'; END IF;
 END LOOP;
 IF patch ? 'amount_breakdown' AND NOT patch ? 'label' THEN
  -- Native legacy imports can keep the old expression inside the label.
  patch:=patch||jsonb_build_object('label',btrim(regexp_replace(regexp_replace(public.tm_payment_title_v18(t.title,'unknown','{}',current_date),'^[0-9]{2}[.][0-9]{2}[[:space:]]+',''),'[[:space:]][–—-][[:space:]]+[^\n]+$','')));
 END IF;
 IF (t.project_data||patch)=t.project_data THEN RAISE EXCEPTION 'no_project_change'; END IF;
 after_title:=public.tm_payment_title_v18(public.tm_project_title_v11((CASE WHEN patch ? 'amount_rub' AND NOT patch ? 'amount_breakdown' THEN t.project_data-'amount_breakdown' ELSE t.project_data END)||patch),t.payment_status,t.project_data,(now() AT TIME ZONE 'Asia/Yekaterinburg')::date);
 stamp:=encode(sha256(convert_to(jsonb_build_array(p_instance,r.id,r.revision,f.version,t.updated_at,patch,p->'evidence')::text,'UTF8')),'hex');
 SELECT * INTO stored FROM tm_api_private.review_proposals_v22 WHERE fingerprint=stamp;
 IF FOUND THEN RETURN jsonb_build_object('prepared',true,'proposal_id',stored.proposal_id,'before_title',stored.before_title,'after_title',stored.after_title,'instruction','Show show_review_question. No project change or user confirmation has occurred.'); END IF;
 pr:=public.tm_propose_project_v11('review-v22:'||stamp,g.group_key,(p->'evidence'->0->>'chat_id')::bigint,t.id,'update',patch,p->'evidence','[]',1,true);
 UPDATE public.tm_project_proposals SET result=jsonb_build_object('review_preparation',true,'review_id',r.id) WHERE id=(pr->>'id')::uuid RETURNING to_jsonb(tm_project_proposals) INTO pr;
 INSERT INTO tm_api_private.review_proposals_v22(proposal_id,instance_id,review_id,review_revision,focus_version,project_id,expected_updated_at,proposal_updated_at,before_title,after_title,patch,evidence,rationale,fingerprint)
 VALUES((pr->>'id')::uuid,p_instance,r.id,r.revision,f.version,t.id,t.updated_at,(pr->>'updated_at')::timestamptz,t.title,after_title,patch,p->'evidence',p->>'rationale',stamp);
 RETURN jsonb_build_object('prepared',true,'proposal_id',pr->>'id','before_title',t.title,'after_title',after_title,'instruction','Show show_review_question. Its update button is the explicit confirmation. Do not apply before that confirmation.');
END $$;

CREATE OR REPLACE FUNCTION public.tm_review_project_preview_v22(p_instance_id text,p_review_id uuid,p_review_revision integer,p_expected_version bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path='' AS $$
DECLARE b tm_api_private.review_proposals_v22; f jsonb; pr public.tm_project_proposals; t public.tasks;
BEGIN
 f:=public.tm_review_focus_get_v18(p_instance_id,'personal-review');
 IF f->>'review_id' IS DISTINCT FROM p_review_id::text OR (f->>'review_revision')::integer IS DISTINCT FROM p_review_revision OR (f->>'version')::bigint IS DISTINCT FROM p_expected_version OR f->>'current' IS DISTINCT FROM 'true' THEN RETURN '{}'; END IF;
 SELECT * INTO b FROM tm_api_private.review_proposals_v22 WHERE instance_id=p_instance_id AND review_id=p_review_id AND review_revision=p_review_revision AND focus_version=p_expected_version ORDER BY created_at DESC,proposal_id DESC LIMIT 1;
 IF NOT FOUND THEN RETURN '{}'; END IF;
 SELECT * INTO pr FROM public.tm_project_proposals WHERE id=b.proposal_id;
 SELECT * INTO t FROM public.tasks WHERE id=b.project_id;
 IF pr.status<>'pending' OR pr.updated_at IS DISTINCT FROM b.proposal_updated_at OR pr.patch IS DISTINCT FROM b.patch OR pr.evidence IS DISTINCT FROM b.evidence OR t.updated_at IS DISTINCT FROM b.expected_updated_at OR NOT public.tm_evidence_valid_v14(t.context_group_id,b.evidence) THEN RETURN jsonb_build_object('prepared',false,'stale',true); END IF;
 RETURN jsonb_build_object('prepared',true,'proposal_id',b.proposal_id,'before_title',b.before_title,'after_title',b.after_title,'rationale',b.rationale,'patch',b.patch,
 'apply_arguments',jsonb_build_object('project_id',b.project_id,'expected_updated_at',b.expected_updated_at,'proposal_id',b.proposal_id,'expected_proposal_updated_at',b.proposal_updated_at,'evidence',b.evidence,'confirmed',true));
END $$;

CREATE OR REPLACE FUNCTION public.tm_api_execute_v22(p_instance_id text,p_request_key text,p_operation text,p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path='' AS $$
DECLARE saved tm_api_private.operations; b tm_api_private.review_proposals_v22; result jsonb; digest bytea; before_row jsonb; after_row jsonb; e jsonb;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' OR NOT public.tm_instance_known_v18(p_instance_id) OR length(coalesce(p_request_key,'')) NOT BETWEEN 16 AND 128 OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'invalid_operation_scope'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('tm_api_v19:'||p_instance_id||':'||p_request_key,0));
 digest:=sha256(convert_to(p_payload::text,'UTF8'));
 SELECT * INTO saved FROM tm_api_private.operations WHERE instance_id=p_instance_id AND request_key=p_request_key;
 IF FOUND THEN
  IF saved.operation IS DISTINCT FROM p_operation OR saved.payload_hash IS DISTINCT FROM digest THEN RAISE EXCEPTION 'idempotency_key_collision'; END IF;
  RETURN saved.result;
 END IF;
 IF p_operation='prepare_review_project_change' THEN
  result:=tm_api_private.prepare_review_v22(p_instance_id,p_payload);
  INSERT INTO tm_api_private.operations(instance_id,request_key,operation,payload_hash,result) VALUES(p_instance_id,p_request_key,p_operation,digest,result);
  RETURN result;
 END IF;
 e:=CASE WHEN p_operation='answer_review_question' THEN p_payload->'effect'->'arguments' ELSE p_payload END;
 IF e ? 'proposal_id' THEN SELECT * INTO b FROM tm_api_private.review_proposals_v22 WHERE proposal_id=(e->>'proposal_id')::uuid; END IF;
 IF b.proposal_id IS NOT NULL THEN
  IF p_operation<>'answer_review_question' OR p_payload->>'action' IS DISTINCT FROM 'resolved' OR p_payload->'effect'->>'operation' IS DISTINCT FROM 'update_existing_project'
   OR b.instance_id IS DISTINCT FROM p_instance_id OR b.review_id IS DISTINCT FROM (p_payload->>'review_id')::uuid OR b.review_revision IS DISTINCT FROM (p_payload->>'review_revision')::integer OR b.focus_version IS DISTINCT FROM (p_payload->>'expected_version')::integer
   OR b.expected_updated_at IS DISTINCT FROM (e->>'expected_updated_at')::timestamptz OR b.proposal_updated_at IS DISTINCT FROM (e->>'expected_proposal_updated_at')::timestamptz OR b.evidence IS DISTINCT FROM e->'evidence'
   THEN RAISE EXCEPTION 'review_proposal_binding_changed'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('review_focus:'||p_instance_id||':personal-review',0));
  PERFORM 1 FROM public.tm_review_focus_state_v18 WHERE instance_id=p_instance_id AND scope_key='personal-review' FOR UPDATE;
  PERFORM 1 FROM public.tm_review_items WHERE id=b.review_id FOR UPDATE;
  PERFORM 1 FROM public.tm_project_proposals WHERE id=b.proposal_id AND patch=b.patch AND evidence=b.evidence FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'proposal_changed'; END IF;
  SELECT to_jsonb(t) INTO before_row FROM public.tasks t WHERE id=b.project_id FOR UPDATE;
  -- Never let a stale/disabled linked chat become valid through a looser group-level guard.
  FOR e IN SELECT value FROM jsonb_array_elements(b.evidence) LOOP
   IF NOT EXISTS(SELECT 1 FROM public.telegram_messages m WHERE m.chat_id=(e->>'chat_id')::bigint AND m.message_id=(e->>'message_id')::bigint AND m.deleted_at IS NULL AND NOT coalesce(m.is_deleted,false)) OR NOT EXISTS(SELECT 1 FROM tm_api_private.project_chats_v21(p_instance_id,b.project_id) c WHERE c.chat_id=(e->>'chat_id')::bigint) THEN RAISE EXCEPTION 'linked_telegram_evidence_required'; END IF;
  END LOOP;
 END IF;
 result:=public.tm_api_execute_v19(p_instance_id,p_request_key,p_operation,p_payload);
 IF b.proposal_id IS NOT NULL THEN
  SELECT to_jsonb(t) INTO after_row FROM public.tasks t WHERE id=b.project_id;
  IF after_row->>'title' IS DISTINCT FROM b.after_title THEN RAISE EXCEPTION 'confirmed_preview_changed'; END IF;
  INSERT INTO public.tm_project_events(task_id,action,actor,before_state,after_state,evidence)
  VALUES(b.project_id,'review_refinement','user_confirmed',jsonb_build_object('title',before_row->'title','project_data',before_row->'project_data'),
   jsonb_build_object('title',after_row->'title','project_data',after_row->'project_data','review_id',b.review_id,'review_revision',b.review_revision,'proposal_id',b.proposal_id,'user_confirmation',p_payload->>'user_text','request_key',p_request_key),b.evidence);
 END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION tm_api_private.prepare_review_v22(text,jsonb),public.tm_api_execute_v22(text,text,text,jsonb),public.tm_review_project_preview_v22(text,uuid,integer,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION tm_api_private.prepare_review_v22(text,jsonb),public.tm_api_execute_v22(text,text,text,jsonb),public.tm_review_project_preview_v22(text,uuid,integer,bigint) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
