"""Read-only HTTPS/MCP smoke check. Never prints credentials or project contents."""
import asyncio
import json
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tm_api.service import READ_TOOLS, WRITE_TOOLS

BASE = "https://service.example.invalid/telegram-manager"


async def main():
    keyfile = Path.home() / "Library/Application Support/TelegramManagerAPI/api.key"
    if keyfile.is_symlink() or keyfile.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe credential file")
    key = keyfile.read_text().strip()
    headers = {"Authorization": "Bearer " + key}
    async with httpx.AsyncClient(timeout=30) as client:
        assert (await client.get(BASE + "/health")).status_code == 401
        health = await client.get(BASE + "/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["api_version"] == "V21"
        assert (await client.post(BASE + "/api/execute_sql", json={}, headers=headers)).status_code == 404
        # Invalid arguments must fail before any database mutation.
        assert (await client.post(BASE + "/api/set_payment_window", json={}, headers=headers)).status_code == 422
    async with streamablehttp_client(BASE + "/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert names == set(READ_TOOLS + WRITE_TOOLS)
            assert all(t.annotations.readOnlyHint == (t.name in READ_TOOLS) for t in tools)
            render=next(t for t in tools if t.name=='show_review_question')
            assert render.meta['ui']['resourceUri']=='ui://telegram-manager/review-v21.html'
            resource=await session.read_resource(render.meta['ui']['resourceUri'])
            assert resource.contents[0].mimeType=='text/html;profile=mcp-app'
            html=resource.contents[0].text
            assert 'ui/message' in html and 'answer_review_question' in html and 'Общий ответ' not in html
            for name, args in [("get_review_submission", {"request_key":"verification-read-only"}), ("get_client_contract", {}), ("get_review_bundle", {}),
                               ("get_current_review_question", {}), ("list_projects", {"limit": 1}), ("get_financial_summary", {"filters": {}})]:
                result = await session.call_tool(name, args)
                assert not result.isError
                assert result.structuredContent is not None
                payload = result.structuredContent or json.loads(next(c.text for c in result.content if c.type=="text"))
                assert isinstance(payload,dict) and payload
                assert "error" not in payload and "error" not in (payload.get("result") or {})
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        current = (await client.post(BASE + "/api/get_current_review_question", json={})).json()
        if current.get("question"):
            q, f = current["question"], current["focus"]
            sources = current["source_context"]["sources"]
            result = await client.post(BASE + "/api/get_review_sources", json={
                "review_id": q["id"], "review_revision": q["revision"], "expected_version": f["version"]})
            assert result.status_code == 200
            result = await client.post(BASE + "/api/get_review_sources", json={
                "review_id": q["id"], "review_revision": q["revision"] + 1, "expected_version": f["version"]})
            assert result.status_code == 409
            if sources.get("chats"):
                pid, chat = sources["project_id"], sources["chats"][0]["chat_id"]
                result = await client.post(BASE + "/api/get_project_sources", json={"project_id":pid})
                assert result.status_code == 200
                result = await client.post(BASE + "/api/search_project_messages", json={"project_id":pid,"chat_id":chat,"limit":2})
                assert result.status_code == 200
                for message in result.json()["messages"][:1]:
                    result = await client.post(BASE + "/api/get_project_message_context", json={
                        "project_id":pid,"chat_id":chat,"message_id":message["message_id"],"radius":1})
                    assert result.status_code == 200 and message.get("content_token")
    print(json.dumps({"ok": True, "api_version": "V21", "https": True, "mcp_read_tools": len(READ_TOOLS), "mcp_write_tools": len(WRITE_TOOLS),
                      "unauthorized_rejected": True, "raw_sql_absent": True,
                      "mac_available": health.json()["mac_available"]}))


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "verification_failed"}))
        raise SystemExit(1) from None
