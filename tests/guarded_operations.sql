\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.assert_true(ok boolean, message text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'FAILED: %',message; END IF; END $$;
CREATE FUNCTION pg_temp.must_fail(query text, expected text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 BEGIN EXECUTE query; EXCEPTION WHEN OTHERS THEN
   IF position(expected in SQLERRM)>0 THEN RETURN; END IF;
   RAISE EXCEPTION 'Expected %, got %',expected,SQLERRM;
 END;
 RAISE EXCEPTION 'Unexpected success: %',expected;
END $$;
INSERT INTO public.telegram_chat_groups(id,group_key,name,reminder_list_instance_id,rules_profile) OVERRIDING SYSTEM VALUE VALUES
 (990001,'test-v19','Synthetic','macbook-owner','projects_payments'),
 (990002,'test-other','Other','other-instance','projects_payments');
INSERT INTO public.integration_health(service_name,instance_id,status,details) OVERRIDING SYSTEM VALUE VALUES
 ('apple-sync','macbook-owner','running','{"version":"V18"}');
INSERT INTO public.tasks(id,title,description,record_kind,context_group_id,project_data,payment_status) OVERRIDING SYSTEM VALUE VALUES
 (990001,'10.20 Synthetic - 40к','Manual notes','project',990001,'{"amount_rub":40000,"work_status":"planned"}','unknown'),
 (990002,'Other','Private','project',990002,'{}','unknown');
INSERT INTO public.tm_review_items(id,item_key,group_id,task_id,kind,title,content_fingerprint) OVERRIDING SYSTEM VALUE VALUES
 ('00000000-0000-0000-0000-000000000001','v19-a',990001,990001,'project','Question A','a'),
 ('00000000-0000-0000-0000-000000000002','v19-b',990001,990001,'project','Question B','b');
DO $$
DECLARE a jsonb; b jsonb; p jsonb; first_result jsonb; before_task jsonb; ts timestamptz; n integer;
BEGIN
 a:='{"review_id":"00000000-0000-0000-0000-000000000001","review_revision":1,"expected_version":0}';
 first_result:=public.tm_api_execute_v19('macbook-owner','set-focus-00000001','set_review_focus',a);
 PERFORM pg_temp.assert_true(first_result=public.tm_api_execute_v19('macbook-owner','set-focus-00000001','set_review_focus',a),'focus duplicate stable');
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','set-focus-00000001','set_review_focus',a||'{"expected_version":1}'),'idempotency_key_collision');
 b:=a||'{"review_id":"00000000-0000-0000-0000-000000000002","expected_version":1}';
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','set-focus-00000002','set_review_focus',b),'resolve_or_release_current_question_first');
 SELECT to_jsonb(t),updated_at INTO before_task,ts FROM public.tasks t WHERE id=990001;
 p:=jsonb_build_object('project_id',990001,'expected_updated_at',ts,'confirmed',true,'window',jsonb_build_object('kind','relative','within_days',7),'evidence','[{"kind":"user","confirmation_ref":"synthetic-user-001","statement":"Payment expected within seven days"}]'::jsonb);
 a:=a||'{"expected_version":1,"action":"resolved","user_text":"Within seven days","confirmed":true}'||jsonb_build_object('effect',jsonb_build_object('operation','set_payment_window','arguments',p));
 -- Failure rolls back both business mutation and review reply.
 b:=jsonb_set(a,'{effect,arguments,window,within_days}','99');
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','answer-focus-0001','answer_review_question',b),'invalid_payment_window');
 PERFORM pg_temp.assert_true((SELECT to_jsonb(t)=before_task FROM public.tasks t WHERE id=990001),'failed answer preserves task');
 PERFORM pg_temp.assert_true((SELECT review_id IS NOT NULL AND version=1 FROM public.tm_review_focus_state_v18 WHERE instance_id='macbook-owner'),'failed answer preserves focus');
 first_result:=public.tm_api_execute_v19('macbook-owner','answer-focus-0001','answer_review_question',a);
 PERFORM pg_temp.assert_true(first_result->'requires_separate_apply'='false','answer applies business action');
 PERFORM pg_temp.assert_true(first_result=public.tm_api_execute_v19('macbook-owner','answer-focus-0001','answer_review_question',a),'lost reply replay after focus advances');
 PERFORM pg_temp.assert_true((SELECT count(*)=1 FROM public.tm_review_decisions),'exactly one decision');
 PERFORM pg_temp.assert_true((SELECT description='Manual notes' AND project_data->>'amount_rub'='40000' AND payment_status='awaiting' FROM public.tasks WHERE id=990001),'notes amount debt preserved');
 PERFORM public.tm_api_execute_v19('macbook-owner','set-focus-00000002','set_review_focus',btrim('{"review_id":"00000000-0000-0000-0000-000000000002","review_revision":1,"expected_version":2}')::jsonb);
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','answer-focus-0002','answer_review_question',a),'review_focus_changed');
 PERFORM pg_temp.assert_true(first_result=public.tm_api_execute_v19('macbook-owner','answer-focus-0001','answer_review_question',a),'replay cannot answer next question');
 -- Stale timestamps, out-of-scope IDs, evidence absence and append-only permissions.
 p:=jsonb_set(p,'{expected_updated_at}','"2000-01-01T00:00:00Z"');
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','window-stale-0001','set_payment_window',p),'project_changed_or_out_of_scope');
 SELECT updated_at INTO ts FROM public.tasks WHERE id=990001;
 p:=p||jsonb_build_object('expected_updated_at',ts,'evidence','[]'::jsonb);
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','window-empty-0001','set_payment_window',p),'evidence_changed_or_missing');
 PERFORM pg_temp.must_fail('DELETE FROM tm_api_private.operations','append_only');
 PERFORM pg_temp.assert_true(NOT has_function_privilege('anon','public.tm_api_execute_v19(text,text,text,jsonb)','execute'),'anon excluded');
 PERFORM pg_temp.assert_true(NOT has_function_privilege('authenticated','public.tm_api_execute_v19(text,text,text,jsonb)','execute'),'authenticated excluded');
 PERFORM pg_temp.assert_true(NOT has_table_privilege('service_role','tm_api_private.operations','update'),'journal cannot be updated by service');
END $$;
DO $$
DECLARE p jsonb; result jsonb; ts timestamptz;
BEGIN
 SELECT updated_at INTO ts FROM public.tasks WHERE id=990001;
 p:=jsonb_build_object('group_key','test-v19','confirmed',true,
 'event',jsonb_build_object('kind','received','amount',40000,'occurred_at','2026-09-28T00:00:00Z'),
 'allocations',jsonb_build_array(jsonb_build_object('task_id',990001,'expected_updated_at',ts,'work_key','main','amount',40000)),
 'evidence','[{"kind":"user","confirmation_ref":"synthetic-payment","statement":"Received forty thousand rubles"}]'::jsonb);
 result:=public.tm_api_execute_v19('macbook-owner','record-payment-0001','record_payment',p);
 PERFORM pg_temp.assert_true(result=public.tm_api_execute_v19('macbook-owner','record-payment-0001','record_payment',p),'payment retry stable');
 PERFORM pg_temp.assert_true((SELECT count(*)=1 FROM public.tm_payment_events),'one payment event');
 PERFORM pg_temp.assert_true((SELECT payment_status='awaiting' FROM public.tasks WHERE id=990001),'ledger does not imply project paid');
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','record-payment-0002','record_payment',p||'{"evidence":[]}'),'evidence_changed_or_missing');
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','record-payment-0003','record_payment',jsonb_set(p,'{allocations,0,task_id}','990002')),'project_changed_or_out_of_scope');
END $$;
INSERT INTO public.telegram_chats(chat_id,chat_name) OVERRIDING SYSTEM VALUE VALUES(-990001,'Synthetic source');
INSERT INTO public.telegram_chat_group_members(group_id,chat_id) OVERRIDING SYSTEM VALUE VALUES(990001,-990001);
INSERT INTO public.telegram_messages(chat_id,message_id,text) OVERRIDING SYSTEM VALUE VALUES(-990001,1,'Confirmed amount forty thousand');
DO $$
DECLARE p jsonb; ev jsonb; ts timestamptz; pid uuid; pts timestamptz; result jsonb;
BEGIN
 SELECT updated_at INTO ts FROM public.tasks WHERE id=990001;
 ev:=jsonb_build_array(jsonb_build_object('kind','telegram','chat_id',-990001,'message_id',1,'content_token',public.tm_content_token_v11(-990001,1)));
 p:=jsonb_build_object('project_id',990001,'expected_updated_at',ts,'window',NULL,'evidence',ev,'confirmed',true);
 UPDATE public.telegram_messages SET text='Edited: not confirmed' WHERE chat_id=-990001 AND message_id=1;
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','stale-source-0001','set_payment_window',p),'evidence_changed_or_missing');
 ev:=jsonb_build_array(jsonb_build_object('kind','telegram','chat_id',-990001,'message_id',1,'content_token',public.tm_content_token_v11(-990001,1)));
 INSERT INTO public.tm_project_proposals(request_key,group_id,chat_id,task_id,kind,patch,evidence,confidence)
 VALUES('synthetic-proposal',990001,-990001,990001,'update','{"label":"Synthetic renamed","date_mmdd":"10.20","amount_rub":40000}',ev,1)
 RETURNING id,updated_at INTO pid,pts;
 p:=jsonb_build_object('project_id',990001,'expected_updated_at',ts,'proposal_id',pid,'expected_proposal_updated_at',pts,'evidence',ev,'confirmed',true);
 result:=public.tm_api_execute_v19('macbook-owner','apply-proposal-0001','update_existing_project',p);
 PERFORM pg_temp.assert_true(result=public.tm_api_execute_v19('macbook-owner','apply-proposal-0001','update_existing_project',p),'proposal replay stable');
 PERFORM pg_temp.assert_true((SELECT description='Manual notes' AND project_data->>'amount_rub'='40000' AND payment_status='awaiting' FROM public.tasks WHERE id=990001),'proposal preserves notes amount debt');
 PERFORM pg_temp.assert_true((SELECT count(*)=2 FROM public.tasks),'proposal creates no duplicate');
 UPDATE public.telegram_messages SET is_deleted=true WHERE chat_id=-990001 AND message_id=1;
 SELECT updated_at INTO ts FROM public.tasks WHERE id=990001;
 p:=jsonb_build_object('project_id',990001,'expected_updated_at',ts,'window',NULL,'evidence',ev,'confirmed',true);
 PERFORM pg_temp.must_fail(format('SELECT public.tm_api_execute_v19(%L,%L,%L,%L::jsonb)','macbook-owner','deleted-source-0001','set_payment_window',p),'evidence_changed_or_missing');
END $$;
ROLLBACK;
SELECT 'V19 guarded transaction checks passed' AS result;
