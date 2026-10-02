-- Optional acceleration. Run off-peak; not inside a transaction.
-- PostgreSQL requires CONCURRENTLY outside BEGIN/COMMIT. Check invalid indexes
-- after an interrupted build before repeating. No historical message is changed.
CREATE INDEX CONCURRENTLY IF NOT EXISTS tm_v24_messages_order
 ON public.telegram_messages ((coalesce(date,created_at)) DESC,chat_id DESC,message_id DESC) WHERE NOT is_deleted;
CREATE INDEX CONCURRENTLY IF NOT EXISTS tm_v24_attachment_text
 ON public.telegram_attachments USING gin (to_tsvector('simple',coalesce(extracted_text,'')||' '||coalesce(transcript,'')||' '||coalesce(content_summary,''))) WHERE deleted_at IS NULL;
