import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from tm_api.v24.common import Rejected
from tm_api.v24.engine import Engine
from tm_api.v24.models import Mutation
from tm_api.v24.register import WRITE_NAMES
from tm_api.whatsapp import WhatsAppReader
from tm_api.whatsapp_backfill import WhatsAppBackfillBusiness


def make_store(path: Path):
    db=sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE chats(
      jid TEXT PRIMARY KEY,name TEXT NOT NULL,search_name TEXT NOT NULL,kind TEXT NOT NULL,
      last_message_at TEXT,updated_at TEXT NOT NULL
    );
    CREATE TABLE messages(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,jid TEXT NOT NULL,message_id TEXT NOT NULL,
      date TEXT NOT NULL,sender_jid TEXT,sender_name TEXT,text TEXT NOT NULL,search_text TEXT NOT NULL,
      reply_to_message_id TEXT,from_me INTEGER NOT NULL,has_media INTEGER NOT NULL,media_type TEXT,
      is_deleted INTEGER NOT NULL,edited_at TEXT,content_token TEXT NOT NULL,updated_at TEXT NOT NULL,
      UNIQUE(jid,message_id)
    );
    """)
    db.executemany("INSERT INTO meta VALUES (?,?)",[
        ("connection_status","connected"),("qr_ready","false"),("account_jid","7900@s.whatsapp.net")
    ])
    db.execute("INSERT INTO chats VALUES (?,?,?,?,?,?)",(
        "group@g.us","Демонстрационный монтажный чат","демонстрационный монтажный чат","group",
        "2026-09-30T10:00:00+00:00","2026-09-30T10:00:00+00:00"))
    db.execute("""INSERT INTO messages(
      jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
      from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
      "group@g.us","oldest","2026-09-29T07:47:50+00:00","sender@lid","","старое","старое",
      None,0,0,None,0,None,"token","2026-09-30T10:00:00+00:00"))
    db.commit()
    db.close()


def mutation():
    return {
        "operation":"create_whatsapp_history_backfill_request",
        "dedupe_key":"wa.backfill.group.oldest",
        "changes":{"chat_id":"group@g.us","count":50},
        "evidence":[{
            "kind":"user",
            "statement":"Подтянуть предыдущую страницу истории WhatsApp для сверки POV.",
            "confirmation_ref":"chat-guarded-backfill",
        }],
    }


def test_mutation_contract_replaces_direct_write_action():
    value=Mutation.model_validate(mutation())
    assert value.operation=="create_whatsapp_history_backfill_request"
    assert "create_whatsapp_history_backfill_request" in WRITE_NAMES
    assert "request_whatsapp_history_backfill" not in WRITE_NAMES


def test_preview_plan_is_read_only_and_post_commit_enqueue_is_idempotent(tmp_path):
    path=tmp_path/"messages.sqlite"
    make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    business=WhatsAppBackfillBusiness("instance",reader)
    m=Mutation.model_validate(mutation()).model_dump(mode="json")

    plan=business.plan(None,m)

    assert plan["after"]["anchor_date"]=="2026-09-29T07:47:50+00:00"
    assert not (tmp_path/"backfill-request.json").exists()

    first=business.post_commit(m,plan)
    assert first["accepted"] is True
    assert first["idempotent"] is False
    assert (tmp_path/"backfill-request.json").exists()

    second=business.post_commit(m,plan)
    assert second["accepted"] is True
    assert second["idempotent"] is True
    assert second["request_id"]==first["request_id"]


def test_anchor_change_requires_new_preview(tmp_path):
    path=tmp_path/"messages.sqlite"
    make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    info=reader.plan_history_backfill("group@g.us",50)

    db=sqlite3.connect(path)
    db.execute("""INSERT INTO messages(
      jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
      from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
      "group@g.us","older","2026-09-20T07:47:50+00:00","sender@lid","","старее","старее",
      None,0,0,None,0,None,"token2","2026-09-30T10:00:00+00:00"))
    db.commit()
    db.close()

    with pytest.raises(Rejected,match="whatsapp_backfill_anchor_changed"):
        reader.enqueue_history_backfill(
            "request-id","group@g.us",50,
            info["anchor_message_id"],info["anchor_date"])


class FakeDatabase:
    def __init__(self):
        self.inside=False

    @contextmanager
    def transaction(self):
        self.inside=True
        try:
            yield object()
        finally:
            self.inside=False


def fake_engine(saved=None):
    engine=Engine.__new__(Engine)
    db=FakeDatabase()
    engine.database=db
    engine.transaction_lock=lambda tx,write: None
    engine.receipt=lambda tx,op,key,args: saved
    row={"request_key":"request-key","payload":[],"plans":[]}
    engine.get_preview=lambda tx,preview_id: row
    engine.apply_in_transaction=lambda tx,pid,digest,ref: {
        "applied":True,"preview_id":pid,"results":[],"atomic":True}
    engine.save_receipt=lambda tx,op,key,args,result: None
    delivered=[]
    def deliver(value):
        assert db.inside is False
        delivered.append(value)
        return [{"operation":"create_whatsapp_history_backfill_request","delivery":{"status":"queued"}}]
    engine.deliver_post_commit=deliver
    return engine,delivered


def test_apply_delivers_only_after_database_commit():
    engine,delivered=fake_engine()
    result=engine.apply("request-key","preview-id","d"*64,True,"confirmation-ref")

    assert len(delivered)==1
    assert result["post_commit"][0]["delivery"]["status"]=="queued"


def test_same_apply_receipt_retries_post_commit_delivery():
    saved={"applied":True,"preview_id":"preview-id","results":[],"atomic":True}
    engine,delivered=fake_engine(saved=saved)
    engine.apply_in_transaction=lambda *args,**kwargs: pytest.fail("must not reapply committed mutation")

    result=engine.apply("request-key","preview-id","d"*64,True,"confirmation-ref")

    assert len(delivered)==1
    assert result["post_commit"][0]["delivery"]["status"]=="queued"
