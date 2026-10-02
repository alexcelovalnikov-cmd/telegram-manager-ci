\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.ok(v boolean,n text) RETURNS void LANGUAGE plpgsql AS $$ BEGIN IF v IS DISTINCT FROM true THEN RAISE EXCEPTION 'FAILED: %',n; END IF; END $$;
CREATE FUNCTION pg_temp.fail(q text,needle text) RETURNS void LANGUAGE plpgsql AS $$ BEGIN BEGIN EXECUTE q; EXCEPTION WHEN OTHERS THEN IF strpos(SQLERRM,needle)>0 THEN RETURN; END IF; RAISE; END; RAISE EXCEPTION 'Unexpected success: %',needle; END $$;
INSERT INTO telegram_chat_groups(id,group_key,name,reminder_list_instance_id,reminder_list_id,rules_profile) OVERRIDING SYSTEM VALUE VALUES(992200,'refinement-test','Synthetic','macbook-owner','synthetic-list','projects_payments');
INSERT INTO integration_health(service_name,instance_id,status,details) VALUES('apple-sync','macbook-owner','running','{"version":"V18"}');
INSERT INTO telegram_chats(chat_id,chat_name) VALUES(-992200,'Synthetic project source');
INSERT INTO telegram_chat_group_members(group_id,chat_id) VALUES(992200,-992200);
INSERT INTO telegram_messages(chat_id,message_id,text,reply_to_message_id) VALUES(-992200,1,'Сколько за цвет считаем по последней Эльбе?',null),(-992200,2,'5 роликов - 5к?',1),(-992200,3,'Ага!',2);
INSERT INTO tasks(id,title,description,status,record_kind,context_group_id,source_chat_id,source_message_id,project_data,payment_status) OVERRIDING SYSTEM VALUE VALUES
(992200,'09.16 Демо Клиент - 15к + ?','Manual notes','completed','project',992200,-992200,1,'{"label":"Демо Клиент - 15к + ?","date_mmdd":"09.16","work_status":"delivered"}','unknown');
INSERT INTO tm_review_items(id,item_key,group_id,task_id,kind,title,context,content_fingerprint) VALUES('00000000-0000-0000-0000-000000002200','project-amount:992200',992200,992200,'incomplete_project','09.16 Демо Клиент - 15к + ?','{"missing":["amount_or_components"]}','unknown-component');
DO $$
DECLARE p jsonb; ev jsonb; prepared jsonb; view jsonb; answer jsonb; res jsonb; t public.tasks; saved jsonb; n integer; oldtoken text;
BEGIN
 SELECT * INTO t FROM tasks WHERE id=992200; saved:=to_jsonb(t);
 SELECT jsonb_agg(jsonb_build_object('kind','telegram','chat_id',chat_id,'message_id',message_id,'content_token',tm_content_token_v11(chat_id,message_id)) ORDER BY message_id) INTO ev FROM telegram_messages WHERE chat_id=-992200;
 PERFORM tm_api_execute_v22('macbook-owner','refinement-focus-0001','set_review_focus','{"review_id":"00000000-0000-0000-0000-000000002200","review_revision":1,"expected_version":0}');
 p:=jsonb_build_object('review_id','00000000-0000-0000-0000-000000002200','review_revision',1,'expected_version',1,'project_id',t.id,'expected_updated_at',t.updated_at,'evidence',ev,'evidence_assessment','supported','rationale','Conversation identifies the additional component; asking user to confirm.',
 'patch','{"amount_rub":"20000.00","amount_status":"stated","amount_breakdown":{"raw":"15к + CC 5k","terms":[{"key":"part_1","raw":"15к","amount":"15000.00","state":"stated"},{"key":"part_2","raw":"CC 5k","label":"CC","amount":"5000.00","state":"stated"}],"complete":true,"known_subtotal":"20000.00","total":"20000.00","proposed_total":"20000.00","source":"reminder_title","confirmation":"imported_unverified"}}'::jsonb);
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-ambig-0001','prepare_review_project_change',p||'{"evidence_assessment":"ambiguous"}'),'evidence_ambiguous');
 PERFORM pg_temp.ok((SELECT count(*)=0 FROM tm_project_proposals),'ambiguous creates no proposal');
 prepared:=tm_api_execute_v22('macbook-owner','refinement-prepare-0001','prepare_review_project_change',p);
 PERFORM pg_temp.ok(prepared->>'after_title'='09.16 Демо Клиент - 15к + CC 5k','concrete preview');
 PERFORM pg_temp.ok((SELECT to_jsonb(x)=saved FROM tasks x WHERE id=t.id),'preparation does not modify project');
 PERFORM pg_temp.ok((SELECT status='open' FROM tm_review_items WHERE task_id=t.id),'preparation does not close review');
 PERFORM pg_temp.ok(prepared=tm_api_execute_v22('macbook-owner','refinement-prepare-0001','prepare_review_project_change',p),'prepare idempotency');
 PERFORM tm_api_execute_v22('macbook-owner','refinement-prepare-0002','prepare_review_project_change',p);
 PERFORM pg_temp.ok((SELECT count(*)=1 FROM tm_project_proposals),'same preview with another key deduplicates');
 view:=tm_review_project_preview_v22('macbook-owner','00000000-0000-0000-0000-000000002200',1,1);
 answer:=jsonb_build_object('review_id',p->'review_id','review_revision',1,'expected_version',1,'action','resolved','user_text','Подтверждаю: 09.16 Демо Клиент - 15к + CC 5k','confirmed',true,'effect',jsonb_build_object('operation','update_existing_project','arguments',view->'apply_arguments'));
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-no-confirm','answer_review_question',answer||'{"confirmed":false}'),'explicit_confirmation_required');
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-wrong-review','answer_review_question',answer||'{"review_revision":2}'),'review_proposal_binding_changed');
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-direct-apply','update_existing_project',view->'apply_arguments'),'review_proposal_binding_changed');
 UPDATE telegram_messages SET text='Нет, сумма другая' WHERE chat_id=-992200 AND message_id=3;
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-stale-source','answer_review_question',answer),'evidence_changed_or_missing');
 UPDATE telegram_messages SET text='Ага!' WHERE chat_id=-992200 AND message_id=3;
 -- Force a failure after apply/resolve: the outer transaction must roll both back.
 CREATE FUNCTION pg_temp.reject_history() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN IF NEW.action=''review_refinement'' THEN RAISE EXCEPTION ''synthetic_history_failure''; END IF; RETURN NEW; END';
 CREATE TRIGGER reject_history BEFORE INSERT ON tm_project_events FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_history();
 PERFORM pg_temp.fail(format('SELECT tm_api_execute_v22(%L,%L,%L,%L::jsonb)','macbook-owner','refinement-answer-0001','answer_review_question',answer),'synthetic_history_failure');
 PERFORM pg_temp.ok((SELECT to_jsonb(x)=saved FROM tasks x WHERE id=t.id),'failed transaction rolls back project');
 PERFORM pg_temp.ok((SELECT status='open' FROM tm_review_items WHERE task_id=t.id),'failed transaction rolls back review');
 DROP TRIGGER reject_history ON tm_project_events;
 res:=tm_api_execute_v22('macbook-owner','refinement-answer-0001','answer_review_question',answer);
 PERFORM pg_temp.ok(res->'decision_saved'='true' AND res->'requires_separate_apply'='false','one atomic confirmed answer');
 PERFORM pg_temp.ok((SELECT title='09.16 Демо Клиент - 15к + CC 5k' AND project_data->>'amount_rub'='20000.00' AND project_data->'amount_breakdown'->'terms'->1->>'label'='CC' AND description='Manual notes' AND payment_status='unknown' AND status='completed' FROM tasks WHERE id=t.id),'title structured components notes work payment preserved');
 PERFORM pg_temp.ok((SELECT status='resolved' FROM tm_review_items WHERE task_id=t.id),'review resolved');
 PERFORM pg_temp.ok((SELECT count(*)=1 FROM tasks),'project not recreated');
 PERFORM pg_temp.ok((SELECT count(*)=1 FROM tm_project_events WHERE action='review_refinement' AND after_state->>'user_confirmation'=answer->>'user_text' AND after_state->>'review_id'=p->>'review_id'),'linked evidence and confirmation history');
 PERFORM pg_temp.ok(res=tm_api_execute_v22('macbook-owner','refinement-answer-0001','answer_review_question',answer),'lost reply replay stable after focus clears');
 PERFORM pg_temp.ok((SELECT count(*)=1 FROM tm_review_decisions),'one review decision');
 PERFORM pg_temp.ok((SELECT count(*)=1 FROM tm_project_events WHERE action='review_refinement'),'one refinement event on retries');
 PERFORM pg_temp.ok(prepared=tm_api_execute_v22('macbook-owner','refinement-prepare-0001','prepare_review_project_change',p),'preparation replay stable after application');
END $$;
SELECT jsonb_build_object('acceptance','? → CC 5k','title',title,'amount_breakdown',project_data->'amount_breakdown','review_status',(SELECT status FROM tm_review_items WHERE task_id=tasks.id),'events',(SELECT count(*) FROM tm_project_events WHERE action='review_refinement'),'decisions',(SELECT count(*) FROM tm_review_decisions)) FROM tasks WHERE id=992200;
ROLLBACK;
