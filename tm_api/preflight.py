"""Run before deployment: python -m tm_api.preflight (environment secrets only)."""
import asyncio
import hashlib
import json
from pathlib import Path
from . import VERSION
from .backend import Backend
from .config import Config
from .service import Service


async def check(backend, config):
    requirements = json.loads(Path(__file__).with_name("v18_rpc_requirements.json").read_text())
    contract = await backend.rpc("tm_contract_v16", {"p_requirements": requirements})
    if contract.get("ok") is not True or contract.get("checked") != len(requirements):
        raise RuntimeError("V18 RPC contract check failed")
    service = Service(backend, config)
    client = await service.get_client_contract()
    if client["display_profile"]["settings"].get("layout") != "cards":
        raise RuntimeError("Existing display profile must be preserved")
    rows, cursor = [], 0
    while True:
        page = await service.list_tasks(limit=100, after_id=cursor, project_only=True)
        rows.extend(page["items"])
        cursor = page["next_after_id"]
        if cursor is None:
            break
        if len(rows) > 100000:
            raise RuntimeError("Preflight pagination limit exceeded")
    # Hash semantic fields, never copy personal data to an audit log.
    data = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"ok": True, "api_version": VERSION, "mac_version": "V18", "read_only": True,
            "rpc_signatures": len(requirements), "projects": len(rows),
            "project_snapshot_sha256": hashlib.sha256(data).hexdigest()}


async def main():
    config = Config.from_env()
    backend = Backend(config)
    try:
        print(json.dumps(await check(backend, config)))
    except Exception:
        print(json.dumps({"ok": False, "error": "preflight_failed"}))
        raise SystemExit(1) from None
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
