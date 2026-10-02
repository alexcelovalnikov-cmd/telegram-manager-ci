import sqlite3
from pathlib import Path

from tm_api.postproduction_approvals import evaluate_postproduction_approval
from tm_api.whatsapp import WhatsAppReader

CHAT="900000554997162426@g.us"
TARGET="asset-message"
APPROVER_A="wa:approver_a"
APPROVER_B="wa:approver_b"


def make_store(path: Path, *, mappings=True):
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
    CREATE TABLE identities(
      identity_key TEXT PRIMARY KEY,display_name TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL
    );
    CREATE TABLE identity_aliases(
      alias TEXT PRIMARY KEY,identity_key TEXT NOT NULL,kind TEXT NOT NULL,updated_at TEXT NOT NULL
    );
    CREATE TABLE approval_identity_map(
      jid TEXT NOT NULL,role TEXT NOT NULL,identity_key TEXT NOT NULL,
      verified_source TEXT NOT NULL,verified_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      PRIMARY KEY(jid,role)
    );
    CREATE TABLE reaction_events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      jid TEXT NOT NULL,target_message_id TEXT NOT NULL,reaction_message_id TEXT NOT NULL,
      actor_identity_key TEXT NOT NULL,actor_jid TEXT,actor_lid TEXT,
      actor_display_name TEXT NOT NULL DEFAULT '',reaction_value TEXT NOT NULL DEFAULT '',
      is_removed INTEGER NOT NULL DEFAULT 0,reacted_at TEXT NOT NULL,source TEXT NOT NULL,
      evidence_token TEXT NOT NULL,updated_at TEXT NOT NULL,
      UNIQUE(jid,reaction_message_id,evidence_token)
    );
    """)
    now="2026-10-01T10:00:00+00:00"
    db.executemany("INSERT INTO meta VALUES (?,?)",[
        ("connection_status","connected"),("qr_ready","false"),("account_jid","7900@s.whatsapp.net")
    ])
    db.execute("INSERT INTO chats VALUES (?,?,?,?,?,?)",(CHAT,"Демонстрационный монтажный чат",
        "демонстрационный монтажный чат","group",now,now))
    db.execute("""INSERT INTO messages(
      jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
      from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
      CHAT,TARGET,now,"editor@lid","Монтажер","POV 12","pov 12",None,0,1,"video",0,None,"msg-token",now
    ))
    db.executemany("INSERT INTO identities VALUES (?,?,?,?)",[
        (APPROVER_A,"Участник А",now,now),(APPROVER_B,"Участник Б",now,now)
    ])
    db.executemany("INSERT INTO identity_aliases VALUES (?,?,?,?)",[
        ("111@s.whatsapp.net",APPROVER_A,"jid",now),("111@lid",APPROVER_A,"lid",now),
        ("222@s.whatsapp.net",APPROVER_B,"jid",now),("222@lid",APPROVER_B,"lid",now),
    ])
    if mappings:
        db.executemany("INSERT INTO approval_identity_map VALUES (?,?,?,?,?,?)",[
            (CHAT,"approver_a",APPROVER_A,"fixture_verified","2026-09-01T00:00:00+00:00",now),
            (CHAT,"approver_b",APPROVER_B,"fixture_verified","2026-09-01T00:00:00+00:00",now),
        ])
    db.commit();db.close()


def add_reaction(path: Path, identity: str, value: str, at: str, rid: str, *, removed=False):
    actor_lid="111@lid" if identity==APPROVER_A else "222@lid"
    display="Участник А" if identity==APPROVER_A else "Участник Б"
    db=sqlite3.connect(path)
    db.execute("""INSERT INTO reaction_events(
      jid,target_message_id,reaction_message_id,actor_identity_key,actor_jid,actor_lid,
      actor_display_name,reaction_value,is_removed,reacted_at,source,evidence_token,updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
      CHAT,TARGET,rid,identity,None,actor_lid,display,value,1 if removed else 0,at,
      "fixture","token-"+rid,at
    ))
    db.commit();db.close()


def reader(path):
    return WhatsAppReader(str(path),"x"*64)


def test_pov_approver_a_black_check_is_fully_approved(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    assert result["state"]=="approved"
    assert result["fully_approved"] is True
    assert result["approvals"]=={"approver_a":True}
    assert result["upload_is_separate_user_action"] is True


def test_non_pov_approver_a_only_is_partial(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).postproduction_approval(CHAT,TARGET,"non_pov")
    assert result["state"]=="partial"
    assert result["fully_approved"] is False
    assert result["approvals"]=={"approver_a":True,"approver_b":False}


def test_non_pov_approver_b_only_is_partial(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_B,"✔️","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).postproduction_approval(CHAT,TARGET,"non_pov")
    assert result["state"]=="partial"
    assert result["fully_approved"] is False
    assert result["approvals"]=={"approver_a":False,"approver_b":True}


def test_non_pov_both_is_fully_approved(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    add_reaction(path,APPROVER_B,"✔","2026-10-01T10:02:00+00:00","r2")
    result=reader(path).postproduction_approval(CHAT,TARGET,"non_pov")
    assert result["state"]=="approved"
    assert result["fully_approved"] is True
    assert result["approvals"]=={"approver_a":True,"approver_b":True}


def test_removed_reaction_revokes_approval(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    before=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    add_reaction(path,APPROVER_A,"","2026-10-01T10:02:00+00:00","r2",removed=True)
    after=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    assert before["fully_approved"] is True
    assert after["state"]=="pending"
    assert after["fully_approved"] is False
    assert after["evidence"][0]["is_removed"] is True
    assert before["reaction_state_token"] != after["reaction_state_token"]


def test_replacement_reaction_revokes_black_check(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    add_reaction(path,APPROVER_A,"❤️","2026-10-01T10:02:00+00:00","r2")
    result=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    assert result["state"]=="pending"
    assert result["fully_approved"] is False
    assert result["evidence"][0]["reaction_value"]=="❤️"


def test_green_check_is_not_black_check(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✅","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    assert result["fully_approved"] is False


def test_identity_must_be_verified_not_inferred_from_display_name(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path,mappings=False)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).postproduction_approval(CHAT,TARGET,"pov")
    assert result["state"]=="identity_unverified"
    assert result["fully_approved"] is False
    assert result["missing_identity_roles"]==["approver_a"]


def test_duplicate_role_identity_fails_closed():
    current=[{
        "actor_identity_key":APPROVER_A,"reaction_value":"✔️","is_removed":False,
        "evidence_token":"x","reacted_at":"2026-10-01T10:00:00+00:00"
    }]
    mappings={
        "approver_a":{"identity_key":APPROVER_A,"verified_source":"fixture","verified_at":"x"},
        "approver_b":{"identity_key":APPROVER_A,"verified_source":"fixture","verified_at":"x"},
    }
    result=evaluate_postproduction_approval(
        jid=CHAT,message_id=TARGET,asset_class="non_pov",workstream="edit",
        current_reactions=current,identity_map=mappings,
    )
    assert result["state"]=="identity_conflict"
    assert result["fully_approved"] is False


def test_color_is_independent_user_owned_workstream(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    add_reaction(path,APPROVER_B,"✔️","2026-10-01T10:02:00+00:00","r2")
    result=reader(path).postproduction_approval(CHAT,TARGET,"non_pov","color")
    assert result["state"]=="independent"
    assert result["approval_applicable"] is False
    assert result["requires_user_confirmation"] is True
    assert result["fully_approved"] is False


def test_message_context_contains_bounded_reaction_evidence_and_enriched_aliases(tmp_path):
    path=tmp_path/"messages.sqlite";make_store(path)
    add_reaction(path,APPROVER_A,"✔️","2026-10-01T10:01:00+00:00","r1")
    result=reader(path).context(CHAT,TARGET,0)
    assert result["reactions"][0]["actor_identity_key"]==APPROVER_A
    assert result["reactions"][0]["actor_jid"]=="111@s.whatsapp.net"
    assert result["reactions"][0]["actor_lid"]=="111@lid"
    assert result["reaction_events"][0]["evidence_token"]=="token-r1"
    assert len(result["reaction_state_token"])==64


def test_collector_has_live_historical_removal_and_identity_paths():
    source=Path("whatsapp/collector.mjs").read_text()
    assert "sock.ev.on('messages.reaction'" in source
    assert "body?.reactionMessage?.key" in source
    assert "reactionValue ? 0 : 1" in source
    assert "lid-mapping.update" in source
    assert "contact?.phoneNumber" in source
    assert "approval_identity_map" in source
    assert "reaction_events" in source
    assert "storeBatch(messages,'history')" in source
    assert "storeBatch(messages,'upsert')" in source
