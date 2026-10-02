import json
import sqlite3
from pathlib import Path

import pytest

from tm_api.v24.common import Rejected
from tm_api.v24.register import WRITE_NAMES
from tm_api.whatsapp import WhatsAppReader


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
    db.execute(
        "INSERT INTO chats VALUES (?,?,?,?,?,?)",
        ("group@g.us","Демонстрационный монтажный чат","демонстрационный монтажный чат","group",
         "2026-09-30T10:00:00+00:00","2026-09-30T10:00:00+00:00")
    )
    db.execute(
        """INSERT INTO messages(
        jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
        from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("group@g.us","oldest","2026-09-29T07:47:50+00:00","sender@lid","","старое",
         "старое",None,0,0,None,0,None,"token","2026-09-30T10:00:00+00:00")
    )
    db.commit()
    db.close()


def test_backfill_request_is_bounded_and_anchored(tmp_path):
    path=tmp_path/"messages.sqlite"
    make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)

    result=reader.request_history_backfill("group@g.us",50)

    assert result["accepted"] is True
    assert result["anchor_message_id"]=="oldest"
    assert result["anchor_date"]=="2026-09-29T07:47:50+00:00"
    request=json.loads((tmp_path/"backfill-request.json").read_text())
    assert request["request_id"]==result["request_id"]
    assert request["jid"]=="group@g.us"
    assert request["count"]==50
    assert (tmp_path/"backfill-request.json").stat().st_mode & 0o777 == 0o600

    status=reader.status()
    assert status["history_backfill"]["status"]=="queued"
    assert status["history_backfill"]["request_id"]==result["request_id"]


def test_backfill_status_prefers_collector_result(tmp_path):
    path=tmp_path/"messages.sqlite"
    make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    request=reader.request_history_backfill("group@g.us",20)
    (tmp_path/"backfill-request.json").unlink()
    (tmp_path/"backfill-result.json").write_text(json.dumps({
        "request_id":request["request_id"],"jid":"group@g.us","status":"received",
        "added":20,"oldest_date":"2026-09-20T10:00:00+00:00"
    }))

    status=reader.status()["history_backfill"]
    assert status["status"]=="received"
    assert status["added"]==20
    assert status["oldest_date"]=="2026-09-20T10:00:00+00:00"


def test_backfill_request_validates_connection_chat_and_count(tmp_path):
    path=tmp_path/"messages.sqlite"
    make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)

    with pytest.raises(Rejected):
        reader.request_history_backfill("missing@g.us",50)
    with pytest.raises(Rejected):
        reader.request_history_backfill("group@g.us",51)

    db=sqlite3.connect(path)
    db.execute("UPDATE meta SET value='disconnected' WHERE key='connection_status'")
    db.commit()
    db.close()
    with pytest.raises(Rejected):
        reader.request_history_backfill("group@g.us",50)


def test_backfill_action_and_collector_contract():
    assert "create_whatsapp_history_backfill_request" in WRITE_NAMES
    assert "request_whatsapp_history_backfill" not in WRITE_NAMES
    collector=Path("whatsapp/collector.mjs").read_text()
    assert "sock.fetchMessageHistory(count,key,timestamp)" in collector
    assert "timeout_no_response" in collector
    assert "received_no_older_messages" in collector
    assert "backfill-request.json" in collector
    assert "backfill-result.json" in collector
