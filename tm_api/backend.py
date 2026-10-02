"""Closed PostgREST adapter. Only named reads; never forwards caller paths or SQL."""
import httpx


class Unavailable(RuntimeError):
    pass


class GuardRejected(ValueError):
    pass


RPC = frozenset({
    "tm_review_project_preview_v22", "tm_project_sources_v21", "tm_review_sources_v21", "tm_search_project_messages_v21", "tm_project_message_context_v21",
    "tm_review_submission_v20", "tm_financial_snapshot_v19", "tm_personal_contract_v18", "tm_review_bundle_v18", "tm_review_focus_get_v18",
    "tm_contract_v16", "tm_review_interaction_contract_v16", "tm_operating_contract_v18",
    "tm_review_display_profile_v15", "tm_payment_evidence_current_v15",
})
TABLES = frozenset({
    "tasks", "telegram_chat_groups", "telegram_chat_group_targets", "integration_health", "tm_project_events",
    "tm_project_proposals", "tm_review_items", "tm_project_finance_v14",
    "tm_salary_series_v15", "tm_jobs_v11", "tm_release_capabilities",
})


class Backend:
    def __init__(self, config, transport=None):
        self.http = httpx.AsyncClient(
            base_url=config.database_url + "/", timeout=15,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"Authorization": "Bearer " + config.database_token},
            follow_redirects=False, transport=transport,
        )

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Never return upstream URLs, headers, exception text or response bodies.
            raise Unavailable("database_unavailable") from None

    async def rpc(self, name, args):
        if name not in RPC:
            raise ValueError("RPC not permitted")
        # GET asks PostgREST to run the stable function in a read-only transaction.
        import json
        params = {k: json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list, bool))
                  else "null" if v is None else str(v) for k, v in args.items()}
        return await self._request("GET", "rpc/" + name, params=params)

    async def rows(self, table, **query):
        if table not in TABLES:
            raise ValueError("Table not permitted")
        return await self._request("GET", table, params=query)

    async def guarded_write(self, instance_id, request_key, operation, payload):
        from .service import WRITE_TOOLS
        if operation not in WRITE_TOOLS:
            raise ValueError("Operation not permitted")
        try:
            response = await self.http.post("rpc/tm_api_execute_v22", json={
                "p_instance_id": instance_id, "p_request_key": request_key,
                "p_operation": operation, "p_payload": payload})
            if response.status_code in (400, 409) and response.json().get("code") in (
                    "P0001", "P0002", "22007", "22008", "22P02", "23514"):
                raise GuardRejected("guard_rejected")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, GuardRejected):
                raise
            raise Unavailable("outcome_unknown_retry_same_key") from None

    async def close(self):
        await self.http.aclose()
