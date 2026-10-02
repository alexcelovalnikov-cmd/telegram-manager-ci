\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.assert_true(ok boolean, label text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'FAILED: %',label; END IF; END $$;
INSERT INTO telegram_chat_groups(id,group_key,name,reminder_list_instance_id,enabled,monitoring_enabled) OVERRIDING SYSTEM VALUE VALUES
(990001,'sources-test','Synthetic','macbook-owner',true,true),(990002,'other','Other','another-owner',true,true);
UPDATE telegram_chat_groups SET rules_profile='projects_payments';
INSERT INTO telegram_chats(chat_id,chat_name,enabled) VALUES(-990001,'Linked',true),(-990002,'Unlinked',true),(-990003,'Disabled',false);
INSERT INTO telegram_chat_group_members(group_id,chat_id,enabled) VALUES(990001,-990001,true),(990001,-990002,true),(990001,-990003,true),(990002,-990001,true);
INSERT INTO integration_health(service_name,instance_id,status,details) VALUES ('apple-sync','macbook-owner','running','{"version":"V18"}');
INSERT INTO tasks(id,title,record_kind,context_group_id,source_chat_id,source_message_id,project_data) OVERRIDING SYSTEM VALUE VALUES
(990001,'Synthetic 15к + ?','project',990001,-990001,10,'{}'),(990002,'Other owner','project',990002,-990001,10,'{}');
INSERT INTO tm_project_links(task_id,chat_id,message_id) VALUES(990001,-990001,10),(990001,-990003,1);
INSERT INTO telegram_messages(chat_id,message_id,text,date,reply_to_message_id,media_source_token) VALUES
(-990001,1,'Earlier parent','2026-09-20',null,null),
(-990001,8,'Before source','2026-09-20',null,null),
(-990001,10,'Synthetic 15к + ?','2026-09-21',1,'current-media'),
(-990001,11,'Дополнительно 5000 за монтаж','2026-09-21',10,null),
(-990001,12,'Literal 50%_quote'' (safe)','2026-09-21',null,null),
(-990001,13,'Deleted answer','2026-09-21',null,null),
(-990001,14,'Deleted by date','2026-09-21',null,null),
(-990002,1,'Unlinked private','2026-09-21',null,null),
(-990003,1,'Disabled private','2026-09-21',null,null);
UPDATE telegram_messages SET is_deleted=true WHERE message_id=13;
UPDATE telegram_messages SET deleted_at=now() WHERE message_id=14;
INSERT INTO telegram_attachments(chat_id,message_id,attachment_index,kind,source_token,source_available,extracted_text) VALUES
(-990001,10,0,'document','current-media',true,'Текущая смета'),
(-990001,10,1,'document','old-media',true,'Старая смета');
INSERT INTO tm_review_items(id,item_key,group_id,task_id,kind,title,content_fingerprint) VALUES
('00000000-0000-0000-0000-000000000021','source-q',990001,990001,'project','Synthetic review','source-q');
DO $$
DECLARE r jsonb; token text; saved jsonb; f jsonb;
BEGIN
 SELECT to_jsonb(t) INTO saved FROM tasks t WHERE id=990001;
 r:=tm_project_sources_v21('macbook-owner',990001);
 PERFORM pg_temp.assert_true(jsonb_array_length(r->'chats')=1,'only enabled explicitly linked chats');
 PERFORM pg_temp.assert_true(r->'linked_messages'->0->>'text'='Synthetic 15к + ?','source present');
 PERFORM pg_temp.assert_true(r::text LIKE '%Дополнительно 5000%','reply automatically available');
 PERFORM pg_temp.assert_true(r::text LIKE '%Earlier parent%','reply parent included');
 PERFORM pg_temp.assert_true(r::text NOT LIKE '%Deleted%' AND r::text NOT LIKE '%Старая%' AND r::text NOT LIKE '%private%','deleted/stale/unlinked excluded');
 PERFORM pg_temp.assert_true(tm_project_sources_v21('macbook-owner',990002)->>'error'='not_found','other-owner project denied');
 PERFORM pg_temp.assert_true(tm_search_project_messages_v21('macbook-owner',990001,-990002)->>'error'='not_found','unlinked chat denied even same group');
 PERFORM pg_temp.assert_true(tm_project_message_context_v21('macbook-owner',990001,-990003,1)->>'error'='not_found','disabled chat denied');
 PERFORM pg_temp.assert_true(tm_project_message_context_v21('macbook-owner',990001,-990001,13)->>'error'='not_found','deleted anchor denied');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'ДОПОЛНИТЕЛЬНО');
 PERFORM pg_temp.assert_true(jsonb_array_length(r->'messages')=1 AND r->'messages'->0->>'message_id'='11','Cyrillic case-insensitive search');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'50%_quote''');
 PERFORM pg_temp.assert_true(jsonb_array_length(r->'messages')=1,'literal punctuation, no wildcard or SQL injection');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'текущая');
 PERFORM pg_temp.assert_true(jsonb_array_length(r->'messages')=1,'current OCR searchable');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'Старая');
 PERFORM pg_temp.assert_true(jsonb_array_length(r->'messages')=0,'stale OCR not searchable');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'',null,2);
 PERFORM pg_temp.assert_true(r->>'next_before_message_id'='11','stable next cursor');
 r:=tm_search_project_messages_v21('macbook-owner',990001,-990001,'',11,2);
 PERFORM pg_temp.assert_true(r->'messages'->0->>'message_id'='10' AND r->>'next_before_message_id'='8','no duplicate page');
 token:=tm_project_message_context_v21('macbook-owner',990001,-990001,10,0)->'messages'->1->>'content_token';
 UPDATE telegram_messages SET text='Edited amount' WHERE chat_id=-990001 AND message_id=10;
 PERFORM pg_temp.assert_true(token IS NOT NULL AND token<>public.tm_content_token_v11(-990001,10),'content token changes on edit');
 UPDATE telegram_chat_group_members SET enabled=false WHERE group_id=990001 AND chat_id=-990001;
 PERFORM pg_temp.assert_true(tm_search_project_messages_v21('macbook-owner',990001,-990001)->>'error'='not_found','disabled membership immediately denied');
 UPDATE telegram_chat_group_members SET enabled=true WHERE group_id=990001 AND chat_id=-990001;
 PERFORM public.tm_api_execute_v19('macbook-owner','source-test-focus-0001','set_review_focus','{"review_id":"00000000-0000-0000-0000-000000000021","review_revision":1,"expected_version":0}');
 r:=tm_review_sources_v21('macbook-owner','00000000-0000-0000-0000-000000000021',1,1);
 PERFORM pg_temp.assert_true(NOT(r ? 'error') AND r->'sources'->>'project_id'='990001','exact review sources');
 PERFORM pg_temp.assert_true(tm_review_sources_v21('macbook-owner','00000000-0000-0000-0000-000000000021',2,1)->>'error'='guard_rejected','stale revision rejected');
 PERFORM pg_temp.assert_true(tm_review_sources_v21('macbook-owner','00000000-0000-0000-0000-000000000021',1,0)->>'error'='guard_rejected','stale focus version rejected');
 PERFORM pg_temp.assert_true(tm_review_sources_v21('macbook-owner','00000000-0000-0000-0000-000000000022',1,1)->>'error'='guard_rejected','wrong question rejected');
 PERFORM pg_temp.assert_true((SELECT to_jsonb(t)=saved FROM tasks t WHERE id=990001),'business project unchanged');
 PERFORM pg_temp.assert_true((SELECT count(*)=0 FROM tm_review_decisions),'reads never answer review');
 PERFORM pg_temp.assert_true(NOT has_function_privilege('anon','public.tm_project_sources_v21(text,bigint)','execute'),'anon has no source access');
 PERFORM pg_temp.assert_true(NOT has_function_privilege('authenticated','public.tm_search_project_messages_v21(text,bigint,bigint,text,bigint,integer)','execute'),'untrusted role has no search access');
END $$;
ROLLBACK;
SELECT 'source isolation, search, replies, freshness, pagination and review guards passed';
