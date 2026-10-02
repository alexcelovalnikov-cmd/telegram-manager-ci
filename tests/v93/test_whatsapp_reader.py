import sqlite3
from pathlib import Path

from tm_api.v24.models import WhatsAppSearch
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
        ("connection_status","connected"),("qr_ready","false"),("account_jid","7900@s.whatsapp.net"),
        ("last_event_at","2026-09-30T10:03:00+00:00"),("last_success_at","2026-09-30T10:03:00+00:00")
    ])
    db.executemany("INSERT INTO chats VALUES (?,?,?,?,?,?)",[
        ("group@g.us","RCC монтаж","rcc монтаж","group","2026-09-30T10:03:00+00:00","2026-09-30T10:03:00+00:00"),
        ("person@s.whatsapp.net","Участник Г","участник г","private","2026-09-30T10:00:00+00:00","2026-09-30T10:00:00+00:00"),
    ])
    rows=[
      ("group@g.us","m1","2026-09-30T10:00:00+00:00","a@s.whatsapp.net","А","Первый бой","первый бой",None,0,0,None,0,None,"t1","2026-09-30T10:00:00+00:00"),
      ("group@g.us","m2","2026-09-30T10:01:00+00:00","b@s.whatsapp.net","Б","Бой смонтирован","бой смонтирован","m1",0,0,None,0,None,"t2","2026-09-30T10:01:00+00:00"),
      ("group@g.us","m3","2026-09-30T10:02:00+00:00","a@s.whatsapp.net","А","Еще сообщение","еще сообщение","m2",0,1,"image",0,None,"t3","2026-09-30T10:02:00+00:00"),
      ("group@g.us","gone","2026-09-30T10:03:00+00:00","a@s.whatsapp.net","А","Удалено","удалено",None,0,0,None,1,None,"t4","2026-09-30T10:03:00+00:00"),
    ]
    db.executemany("""INSERT INTO messages(
      jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
      from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",rows)
    db.commit();db.close()


def test_status_and_chat_catalog(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    status=reader.status()
    assert status["connected"] is True
    assert status["messages"]==3
    result=reader.chats(50,"монтаж")
    assert [x["jid"] for x in result["items"]]==["group@g.us"]


def test_search_is_bounded_casefolded_and_excludes_deleted(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    result=reader.search(WhatsAppSearch(query="СМОНТИРОВАН",limit=2,include_context=False))
    assert [x["message_id"] for x in result["items"]]==["m2"]
    assert result["items"][0]["content_token"]=="t2"
    deleted=reader.search(WhatsAppSearch(query="удалено",limit=10,include_context=False))
    assert deleted["items"]==[]


def test_search_cursor_and_context(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    reader=WhatsAppReader(str(path),"x"*64)
    first=reader.search(WhatsAppSearch(query="",chat_ids=["group@g.us"],limit=2,include_context=False))
    assert [x["message_id"] for x in first["items"]]==["m3","m2"]
    assert first["has_more"] is True and first["next_cursor"]
    second=reader.search(WhatsAppSearch(query="",chat_ids=["group@g.us"],limit=2,cursor=first["next_cursor"],include_context=False))
    assert [x["message_id"] for x in second["items"]]==["m1"]
    ctx=reader.context("group@g.us","m2",1)
    assert ctx["before"][0]["message_id"]=="m1"
    assert ctx["after"][0]["message_id"]=="m3"
    assert ctx["reply_chain"][0]["message_id"]=="m1"
    assert ctx["direct_replies"][0]["message_id"]=="m3"


def test_missing_store_is_optional(tmp_path):
    reader=WhatsAppReader(str(tmp_path/"missing.sqlite"),"x"*64)
    assert reader.status()["status"]=="not_linked"
    assert reader.chats()["items"]==[]


def test_status_reports_bounded_tor_bootstrap_diagnostics(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    (tmp_path/"tor-bootstrap.log").write_text(
        "[notice] Bootstrapped 5% (conn): Connecting to a relay\n"
        "[warn] Problem bootstrapping. Stuck at 10% (conn_done): Connected to a relay. (Connection refused; CONNECTREFUSED; count 1; recommendation warn; host x)\n"
        "[notice] Bootstrapped 10% (conn_done): Connected to a relay\n"
    )
    status=WhatsAppReader(str(path),"x"*64).status()
    assert status["transport_bootstrap"]=="pending_or_failed"
    assert status["transport_bootstrap_percent"]==10
    assert "Problem bootstrapping" in status["transport_error"]


def test_status_reports_ready_tor_bootstrap(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    (tmp_path/"tor-bootstrap.log").write_text("[notice] Bootstrapped 100% (done): Done\n")
    status=WhatsAppReader(str(path),"x"*64).status()
    assert status["transport_bootstrap"]=="ready"
    assert status["transport_bootstrap_percent"]==100
    assert status["transport_error"] is None


def test_shared_worker_tor_ignores_stale_legacy_bootstrap_log(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    db=sqlite3.connect(path)
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",[
        ("transport","tor_socks"),
        ("transport_topology","shared_worker_tor"),
        ("qr_ready","true"),
    ])
    db.commit();db.close()
    (tmp_path/"tor-bootstrap.log").write_text("[notice] Bootstrapped 10% (conn_done): stale legacy log\n")
    status=WhatsAppReader(str(path),"x"*64).status()
    assert status["transport_topology"]=="shared_worker_tor"
    assert status["transport_bootstrap"]=="ready"
    assert status["transport_bootstrap_percent"]==100
    assert status["transport_error"] is None


def test_shared_worker_tor_reports_current_local_socks_failure_without_stale_percent(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    db=sqlite3.connect(path)
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",[
        ("transport","tor_socks"),
        ("transport_topology","shared_worker_tor"),
        ("connection_status","disconnected"),
        ("qr_ready","false"),
        ("last_disconnect_reason","WebSocket Error (connect ECONNREFUSED 127.0.0.1:9050)"),
    ])
    db.commit();db.close()
    (tmp_path/"tor-bootstrap.log").write_text("[notice] Bootstrapped 10% (conn_done): stale legacy log\n")
    status=WhatsAppReader(str(path),"x"*64).status()
    assert status["transport_bootstrap"]=="pending_or_failed"
    assert status["transport_bootstrap_percent"] is None
    assert status["transport_error"]=="shared Tor SOCKS endpoint unavailable"
