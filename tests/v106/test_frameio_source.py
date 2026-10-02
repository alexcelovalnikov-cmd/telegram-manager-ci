import json
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tm_api.frameio import (
    FrameIOError,
    FrameIOOAuth,
    FrameIOReader,
    bind_frameio_project_to_umbrella,
)
from tm_api.security import sanitize
from tm_api.v24.register import READ_NAMES, WRITE_NAMES


class StaticOAuth:
    def status(self):
        return {"enabled": True, "authorized": True, "read_only_source": True, "tokens_exposed": False}

    def access_token(self):
        return "a" * 40


def test_oauth_state_and_tokens_stay_server_side(tmp_path):
    secret=tmp_path/"client-secret"
    secret.write_text("s"*40)
    os.chmod(secret,0o600)
    state_path=tmp_path/"frameio"/"oauth.json"
    seen=[]

    def handler(request):
        seen.append(request)
        assert request.url.host=="ims-na1.adobelogin.com"
        assert request.headers["authorization"].startswith("Basic ")
        body=parse_qs(request.content.decode())
        assert body["grant_type"]==["authorization_code"]
        return httpx.Response(200,json={
            "access_token":"a"*40,
            "refresh_token":"r"*40,
            "expires_in":3600,
            "scope":"openid profile offline_access",
        })

    oauth=FrameIOOAuth(
        enabled=True,
        client_id="client-id",
        client_secret_path=str(secret),
        state_path=str(state_path),
        redirect_uri="https://example.test/telegram-manager/frameio/oauth/callback",
        clock=lambda:1000,
        transport=httpx.MockTransport(handler),
    )
    authorization_url=oauth.begin()
    state=parse_qs(urlsplit(authorization_url).query)["state"][0]
    pending=state_path.read_text()
    assert state not in pending
    assert "pending_state_digest" in pending
    result=oauth.complete("c"*32,state)
    assert result=={"authorized":True,"read_only_source":True,"tokens_exposed":False}
    assert state_path.stat().st_mode & 0o077 == 0
    status=oauth.status()
    assert status["authorized"] is True
    assert status["tokens_exposed"] is False
    assert "access_token" not in status and "refresh_token" not in status
    assert seen


def test_oauth_refresh_rotates_server_state_without_exposure(tmp_path):
    secret=tmp_path/"secret"
    secret.write_text("s"*40)
    state_path=tmp_path/"oauth.json"
    state_path.write_text(json.dumps({
        "access_token":"old-access-"+"a"*30,
        "refresh_token":"old-refresh-"+"r"*30,
        "expires_at":900,
    }))
    os.chmod(state_path,0o600)

    def handler(request):
        body=parse_qs(request.content.decode())
        assert body["grant_type"]==["refresh_token"]
        assert body["refresh_token"][0].startswith("old-refresh-")
        return httpx.Response(200,json={
            "access_token":"new-access-"+"a"*30,
            "refresh_token":"new-refresh-"+"r"*30,
            "expires_in":3600,
        })

    oauth=FrameIOOAuth(
        enabled=True,client_id="client-id",client_secret_path=str(secret),
        state_path=str(state_path),redirect_uri="https://example.test/callback",
        clock=lambda:1000,transport=httpx.MockTransport(handler),
    )
    assert oauth.access_token().startswith("new-access-")
    saved=json.loads(state_path.read_text())
    assert saved["refresh_token"].startswith("new-refresh-")
    status=oauth.status()
    assert {"access_token","refresh_token","id_token"}.isdisjoint(status)
    assert status["tokens_exposed"] is False


def test_reader_uses_bounded_v4_pagination_and_omits_signed_urls():
    calls=[]

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/folders/folder-1/children"):
            assert request.url.params["page_size"]=="2"
            return httpx.Response(200,json={
                "data":[{
                    "id":"file-1","name":"Intro.mov","type":"file","project_id":"project-1",
                    "adobe_version_id":"7","created_at":"2026-07-04T10:00:00Z",
                    "updated_at":"2026-07-04T10:10:00Z",
                    "view_url":"https://signed.example/view",
                    "media_links":{"original":{"download_url":"https://signed.example/file"}},
                }],
                "links":{"next":"/v4/accounts/account-1/folders/folder-1/children?after=cursor-2&page_size=2"},
            })
        raise AssertionError(request.url.path)

    reader=FrameIOReader(StaticOAuth(),transport=httpx.MockTransport(handler))
    result=reader.folder_children("account-1","folder-1",2)
    assert result["items"][0]["id"]=="file-1"
    assert result["items"][0]["adobe_version_id"]=="7"
    assert "view_url" not in result["items"][0]
    assert "media_links" not in result["items"][0]
    assert result["next_cursor"]=="cursor-2"
    assert result["has_more"] is True
    assert calls[0].method=="GET"


def test_version_stack_comments_and_search_preserve_exact_identity():
    def handler(request):
        path=request.url.path
        if path.endswith("/version_stacks/stack-1") and not path.endswith("/children"):
            return httpx.Response(200,json={"data":{
                "id":"stack-1","name":"Intro","project_id":"project-1","parent_id":"folder-1",
                "head_version":{
                    "id":"file-v3","name":"Intro_v3.mov","project_id":"project-1",
                    "adobe_version_id":"3","created_at":"2026-07-04T12:00:00Z",
                    "updated_at":"2026-07-04T12:30:00Z",
                    "view_url":"https://signed.example/head",
                },
            }})
        if path.endswith("/version_stacks/stack-1/children"):
            assert request.url.params["page_size"]=="100"
            return httpx.Response(200,json={"data":[
                {"id":"file-v2","name":"Intro_v2.mov","project_id":"project-1",
                 "adobe_version_id":"2","created_at":"2026-07-04T11:00:00Z","updated_at":"2026-07-04T11:30:00Z"},
                {"id":"file-v3","name":"Intro_v3.mov","project_id":"project-1",
                 "adobe_version_id":"3","created_at":"2026-07-04T12:00:00Z","updated_at":"2026-07-04T12:30:00Z"},
            ],"links":{"next":None}})
        if path.endswith("/files/file-v3/comments"):
            assert request.url.params["include"]=="owner"
            return httpx.Response(200,json={"data":[{
                "id":"comment-1","file_id":"file-v3","text":"Поправить первый кадр",
                "timestamp":"00:00:05:03",
                "created_at":"2026-07-04T12:40:00Z","updated_at":"2026-07-04T12:41:00Z",
                "owner":{"id":"user-1","name":"Fedya","email":"private@example.test",
                         "avatar_url":"https://signed.example/avatar"},
            }],"links":{"next":None}})
        if path.endswith("/accounts/account-1/search"):
            assert request.method=="POST"
            body=json.loads(request.content)
            assert body=={"query":"Intro_2026.07.04_RCC_MMA_25","engine":"lexical",
                          "filters":{"files_and_version_stacks":True,"folders":True,"projects":True}}
            assert request.url.params["page_size"]=="10"
            return httpx.Response(200,json={"data":[{
                "result":{"id":"file-v3","name":"Intro_2026.07.04_RCC_MMA_25","type":"file",
                          "project_id":"project-1","view_url":"https://signed.example"},
                "matches":[{"type":"name_match","name":"Intro_2026.07.04_RCC_MMA_25"}],
            }],"links":{"next":None}})
        raise AssertionError(path)

    reader=FrameIOReader(StaticOAuth(),transport=httpx.MockTransport(handler))
    stack=reader.version_stack("account-1","stack-1")
    assert stack["current_version"]["id"]=="file-v3"
    assert stack["current_version"]["adobe_version_id"]=="3"
    assert stack["current_version_exact"] is True
    assert stack["children_complete"] is True
    assert "view_url" not in stack["current_version"]
    assert stack["versions"][0]["id"]=="file-v2"
    assert [v["stack_position"] for v in stack["versions"]]==[1,2]

    comments=reader.comments("account-1","file-v3")
    comment=comments["items"][0]
    assert comment["id"]=="comment-1"
    assert comment["author"]=={"id":"user-1","name":"Fedya"}
    assert comment["timestamp"]=="00:00:05:03"
    assert comments["timestamp_format"]=="frameio_timecode_HH:MM:SS:FF"
    assert "email" not in comment["author"]

    search=reader.search("account-1","Intro_2026.07.04_RCC_MMA_25",10)
    assert search["items"][0]["id"]=="file-v3"
    assert search["coverage"]["bounded"] is True
    assert "view_url" not in search["items"][0]


def test_frameio_adapter_has_no_generic_write_surface():
    reader=FrameIOReader(StaticOAuth(),transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(FrameIOError,match="frameio_write_not_allowed"):
        reader._request("POST","/accounts/account-1/files/file-1/comments",json_body={"data":{"text":"write"}})
    assert not any("frameio" in name and name in WRITE_NAMES for name in WRITE_NAMES)
    expected={
        "get_frameio_status","list_frameio_accounts","list_frameio_workspaces","list_frameio_projects",
        "get_frameio_project","get_frameio_folder","list_frameio_folder_children","get_frameio_file",
        "list_frameio_version_stacks","get_frameio_version_stack","list_frameio_comments",
        "search_frameio_evidence",
    }
    assert expected.issubset(set(READ_NAMES))


def test_rcc_mma_two_frameio_files_bind_one_existing_umbrella():
    umbrellas=[{
        "id":"umbrella-rcc-mma-0407",
        "data":{"title":"RCC MMA 04.07.2026",
                "source_chats":["Демонстрационный монтажный чат"]},
    }]
    project={"id":"frame-project-rcc-mma-0407","name":"RCC MMA 04.07.2026"}
    files=[
        {"id":"frame-file-backstage","name":"Backstage_2026.07.04_RCC_MMA_25"},
        {"id":"frame-file-intro","name":"Intro_2026.07.04_RCC_MMA_25"},
    ]
    result=bind_frameio_project_to_umbrella(umbrellas,project,files)
    assert result["umbrella_binding"]=={
        "status":"bind_existing","entity_id":"umbrella-rcc-mma-0407",
    }
    assert result["creates_umbrella"] is False
    assert result["source_role"]=="supplemental_evidence"
    assert {item["umbrella_entity_id"] for item in result["assets"]}=={"umbrella-rcc-mma-0407"}
    assert {item["frameio_file_id"] for item in result["assets"]}=={
        "frame-file-backstage","frame-file-intro",
    }


def test_ambiguous_frameio_project_identity_never_selects_or_creates_umbrella():
    umbrellas=[
        {"id":"one","data":{"title":"RCC MMA 04.07.2026"}},
        {"id":"two","data":{"title":"RCC MMA 04.07.2026"}},
    ]
    result=bind_frameio_project_to_umbrella(
        umbrellas,{"id":"project","name":"RCC MMA 04.07.2026"},
        [{"id":"file","name":"Intro_2026.07.04_RCC_MMA_25"}],
    )
    assert result["umbrella_binding"]["status"]=="ambiguous"
    assert result["assets"][0]["umbrella_entity_id"] is None
    assert result["creates_umbrella"] is False


def test_sanitize_removes_frameio_token_fields():
    value=sanitize({
        "access_token":"secret-a",
        "refresh_token":"secret-r",
        "id_token":"secret-i",
        "nested":{"code_verifier":"secret-v","safe":"ok"},
    })
    assert value=={"nested":{"safe":"ok"}}
