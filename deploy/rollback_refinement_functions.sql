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
  IF e->>'content_token' IS DISTINCT FROM public.tm_content_token_v11(pr.chat_id,(e->>'message_id')::bigint) OR NOT EXISTS(SELECT 1 FROM public.telegram_messages WHERE chat_id=pr.chat_id AND message_id=(e->>'message_id')::bigint AND NOT is_deleted) THEN RAISE EXCEPTION 'Evidence edited/deleted; review again'; END IF;
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
  data:=t.project_data||(pr.patch-'payment_status'); rendered_title:=CASE WHEN pr.patch ?| ARRAY['label','date_mmdd','date_iso','amount_rub'] THEN public.tm_project_title_v11(data) ELSE t.title END;
  work:=coalesce(data->>'work_status',CASE WHEN t.status='completed' THEN 'delivered' ELSE 'planned' END); pay:=coalesce(pr.patch->>'payment_status',t.payment_status);
  IF t.payment_status='paid' AND pay<>'paid' THEN RAISE EXCEPTION 'Reversing a confirmed payment requires separate reconciliation'; END IF;
  IF p_remove_paid_reminder AND pay<>'paid' THEN RAISE EXCEPTION 'Removal only after actual payment'; END IF;
  UPDATE public.tasks SET title=rendered_title,project_data=data,status=CASE WHEN work='delivered' THEN 'completed' ELSE 'open' END,completed_at=CASE WHEN work='delivered' THEN coalesce(completed_at,now()) ELSE NULL END,payment_status=pay,reminder_flagged=CASE WHEN pay='awaiting' THEN true ELSE reminder_flagged END,payment_confirmed_at=CASE WHEN pay='paid' THEN coalesce(payment_confirmed_at,now()) ELSE payment_confirmed_at END,payment_evidence=CASE WHEN pay='paid' THEN jsonb_build_object('user_confirmed',p_user_confirmed,'chat_id',pr.chat_id,'messages',pr.evidence,'proposal_id',pr.id) ELSE payment_evidence END,project_delete_request=CASE WHEN p_remove_paid_reminder THEN jsonb_build_object('approved',true,'state','approved','proposal_id',pr.id,'approved_at',now()) ELSE project_delete_request END,updated_at=now() WHERE id=t.id;
 END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(pr.evidence) LOOP INSERT INTO public.tm_project_links(task_id,chat_id,message_id) VALUES(t.id,pr.chat_id,(e->>'message_id')::bigint) ON CONFLICT DO NOTHING; END LOOP;
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
 IF EXISTS(SELECT 1 FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('label','date_mmdd','date_iso','amount_rub','work_status','payment_status')) THEN RAISE EXCEPTION 'Unexpected project patch field'; END IF;
 IF p_patch->>'work_status' IS NOT NULL AND p_patch->>'work_status' NOT IN ('planned','in_progress','delivered') THEN RAISE EXCEPTION 'Invalid work state'; END IF;
 IF p_patch->>'payment_status' IS NOT NULL AND p_patch->>'payment_status' NOT IN ('unknown','awaiting','partial','paid') THEN RAISE EXCEPTION 'Invalid payment state'; END IF;
 IF p_task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.tasks WHERE id=p_task_id AND context_group_id=gid AND record_kind='project' AND project_archived_at IS NULL) THEN RAISE EXCEPTION 'Project not in this group'; END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(p_evidence) LOOP
  IF e->>'content_token' IS NULL OR e->>'content_token' IS DISTINCT FROM public.tm_content_token_v11(p_chat_id,(e->>'message_id')::bigint) OR NOT EXISTS(SELECT 1 FROM public.telegram_messages WHERE chat_id=p_chat_id AND message_id=(e->>'message_id')::bigint AND NOT is_deleted) THEN RAISE EXCEPTION 'Proposal evidence is not current'; END IF;
 END LOOP;
 INSERT INTO public.tm_project_proposals(request_key,group_id,chat_id,task_id,kind,patch,evidence,match_candidates,confidence,history_only) VALUES(p_request_key,gid,p_chat_id,p_task_id,p_kind,p_patch,p_evidence,coalesce(p_matches,'[]'),greatest(0,least(1,p_confidence)),p_history_only) ON CONFLICT(request_key) DO NOTHING;
 SELECT * INTO STRICT pr FROM public.tm_project_proposals WHERE request_key=p_request_key;
 IF pr.group_id<>gid OR pr.chat_id<>p_chat_id OR pr.task_id IS DISTINCT FROM p_task_id OR pr.kind<>p_kind OR pr.patch<>p_patch OR pr.evidence<>p_evidence THEN RAISE EXCEPTION 'Proposal key collision'; END IF;
 RETURN to_jsonb(pr);
END $function$
;
