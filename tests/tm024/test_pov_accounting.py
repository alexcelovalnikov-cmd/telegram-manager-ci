from datetime import datetime
from types import SimpleNamespace

import pytest

from tm_api.postproduction_pov_accounting import (
    CONTRACT,
    PovAccountingBusiness,
    _next_title,
    _pov_block_rows,
)
from tm_api.v24.common import Rejected
from tm_api.v24.models import Operation
from tm_api.v24.register import MUTATION_NAMES, WRITE_NAMES
from tm_api.v25.register import READ_NAMES


POST = {
    "id": "b39f14f1-3d21-440e-ab14-bc63db4ae304",
    "workspace_key": "postproduction",
    "status": "active",
    "settings": {"analysis": {"profile": "postproduction"}, "timezone": "Asia/Yekaterinburg"},
    "revision": 6,
}
RCC = {
    "id": "acb7ad84-cf4d-4a0c-96ef-698646ad4919",
    "workspace_key": "rcc",
    "status": "active",
    "settings": {"analysis": {"profile": "rcc_settlement"}},
    "revision": 8,
}
INTEGRATION = {
    "mode": "rcc_settlement",
    "enabled": True,
    "write_mode": "managed",
    "spreadsheet_id": "18HKmfN8I9bLH2hgRhgUbZzcK00Tf160NdFr3krqUhPk",
    "sheet_name": "Лист1",
}
RULE = {
    "id": "27936ca1-66e2-4da8-a5f5-7e03f182d0fc",
    "revision": 1,
    "capacity": 6,
    "rows": [
        {"label": "1я половина", "payer": "Участник А", "amount_rub": 15000},
        {"label": "2я половина", "payer": "Участник Б", "amount_rub": 15000},
    ],
    "next_block_counter": 0,
    "include_in_total_immediately": True,
    "create_next_block_before_total": True,
}


def asset(n):
    return {
        "asset_id": f"00000000-0000-4000-8000-{n:012d}",
        "entity_id": f"10000000-0000-4000-8000-{n:012d}",
        "sheet_id": 1378800391,
        "sheet_row_number": 150 + n,
        "event_key": f"2026.09.12_RCC_HARD_POV_{n}",
        "layout_number": n,
    }


def state(*, counter=0, anchored=None, processed=None, contributions=None):
    return {
        "contract_version": CONTRACT,
        "rcc_workspace_id": RCC["id"],
        "rule_id": RULE["id"],
        "rule_revision": RULE["revision"],
        "capacity": 6,
        "activation_anchor_asset_ids": list(anchored or []),
        "processed_asset_ids": list(processed or []),
        "active_block": {
            "title": "Монтаж 6 роликов POV сентябрь",
            "sheet_row": 177,
            "baseline_counter": 0,
            "counter": counter,
            "contributions": list(contributions or []),
        },
        "closed_blocks": [],
        "overrides": [],
    }


class StubBusiness(PovAccountingBusiness):
    def __init__(self, assets, state_data=None, counter=0):
        super().__init__("macbook-owner", "/fake.json")
        self.stub_assets = assets
        self.stub_state = state_data
        self.stub_counter = counter

    def _workspaces(self, tx, m):
        return POST, RCC, INTEGRATION

    def _rule(self, tx, rcc_workspace_id):
        return RULE

    def _assets(self, tx, workspace_id):
        return self.stub_assets

    def _state(self, tx, workspace_id, lock):
        if self.stub_state is None:
            return "61eb3272-42cf-4606-bd41-33345a767193", None
        return "61eb3272-42cf-4606-bd41-33345a767193", {
            "data": self.stub_state, "revision": 3, "status": "active"
        }

    def _live_sheet(self, integration):
        return {
            "spreadsheet_id": INTEGRATION["spreadsheet_id"],
            "sheet_name": "Лист1",
            "sheet_id": 0,
            "rows": [],
            "blocks": [],
            "current": {
                "title": "Монтаж 6 роликов POV сентябрь",
                "row": 177,
                "counter": self.stub_counter,
            },
            "total_index": 191,
            "total_amount_col": 2,
            "total_amount": 126040,
            "total_label_col": 1,
        }


def mutation(overrides=None, user_evidence=True):
    evidence = [{"kind": "user", "statement": "Явное подтверждение override"}] if user_evidence else [
        {"kind": "telegram", "chat_id": 1, "message_id": 1}
    ]
    return {
        "operation": "update_pov_accounting_projection",
        "target_id": POST["id"],
        "workspace_id": POST["id"],
        "expected_revision": 6,
        "changes": {
            "rcc_workspace_id": RCC["id"],
            "overrides": list(overrides or []),
        },
        "evidence": evidence,
    }


def test_first_activation_adopts_zero_and_anchors_existing_assets():
    existing = [asset(1), asset(3)]
    plan = StubBusiness(existing, None, 0).plan(None, mutation())
    assert plan["_diagnostics"]["action"] == "activate_baseline"
    assert plan["_state_data"]["active_block"]["counter"] == 0
    assert plan["_state_data"]["activation_anchor_asset_ids"] == [
        existing[0]["asset_id"], existing[1]["asset_id"]
    ]
    assert plan["_state_data"]["processed_asset_ids"] == []
    assert plan["_delivery"] is None


def test_existing_anchor_is_never_retroactively_counted():
    existing = [asset(1), asset(3)]
    s = state(anchored=[x["asset_id"] for x in existing])
    with pytest.raises(Rejected, match="pov_accounting_projection_no_changes"):
        StubBusiness(existing, s, 0).plan(None, mutation())


def test_new_asset_increments_exactly_once():
    old = asset(1)
    new = asset(2)
    s = state(anchored=[old["asset_id"]])
    plan = StubBusiness([old, new], s, 0).plan(None, mutation())
    assert plan["_state_data"]["active_block"]["counter"] == 1
    assert plan["_state_data"]["processed_asset_ids"] == [new["asset_id"]]
    assert plan["_delivery"]["mode"] == "update_counter"
    assert plan["_delivery"]["before_counter"] == 0
    assert plan["_delivery"]["after_counter"] == 1

    repeated = plan["_state_data"]
    with pytest.raises(Rejected, match="pov_accounting_projection_no_changes"):
        StubBusiness([old, new], repeated, 1).plan(None, mutation())


def test_double_count_requires_user_evidence_and_adds_two():
    old = asset(1)
    new = asset(2)
    s = state(anchored=[old["asset_id"]])
    override = {
        "asset_id": new["asset_id"],
        "kind": "double_count",
        "evidence_text": "Пользователь явно сказал считать этот POV за два.",
    }
    with pytest.raises(Rejected, match="pov_accounting_override_requires_user_evidence"):
        StubBusiness([old, new], s, 0).plan(None, mutation([override], user_evidence=False))
    plan = StubBusiness([old, new], s, 0).plan(None, mutation([override]))
    assert plan["_state_data"]["active_block"]["counter"] == 2
    assert plan["_state_data"]["active_block"]["contributions"][0]["units"] == 2
    assert plan["_state_data"]["overrides"][0]["kind"] == "double_count"


def test_short_close_requires_explicit_override_and_opens_next_block():
    old = asset(1)
    new = asset(2)
    s = state(counter=3, anchored=[old["asset_id"]])
    override = {
        "asset_id": new["asset_id"],
        "kind": "close_short",
        "evidence_text": "Пользователь явно закрыл этот укороченный блок.",
    }
    plan = StubBusiness([old, new], s, 3).plan(None, mutation([override]))
    assert plan["_delivery"]["mode"] == "close_and_open"
    assert plan["_delivery"]["final_counter"] == 4
    assert plan["_state_data"]["closed_blocks"][-1]["close_reason"] == "explicit_short_block"
    assert plan["_state_data"]["active_block"]["counter"] == 0
    assert plan["_delivery"]["after_total"] == 156040.0


def test_six_normal_assets_close_standard_block():
    old = asset(1)
    new_assets = [asset(i) for i in range(2, 8)]
    s = state(anchored=[old["asset_id"]])
    plan = StubBusiness([old] + new_assets, s, 0).plan(None, mutation())
    assert plan["_delivery"]["mode"] == "close_and_open"
    assert plan["_delivery"]["final_counter"] == 6
    assert plan["_state_data"]["closed_blocks"][-1]["close_reason"] == "capacity_reached"
    assert len(plan["_state_data"]["closed_blocks"][-1]["contributions"]) == 6
    assert plan["_state_data"]["active_block"]["counter"] == 0


def test_overflow_fails_closed_instead_of_spilling_implicitly():
    old = asset(1)
    new = asset(2)
    s = state(counter=5, anchored=[old["asset_id"]])
    override = {
        "asset_id": new["asset_id"],
        "kind": "double_count",
        "evidence_text": "Считать за два.",
    }
    with pytest.raises(Rejected, match="pov_accounting_block_overflow"):
        StubBusiness([old, new], s, 5).plan(None, mutation([override]))


def test_sheet_parser_and_month_title_are_deterministic():
    rows = [
        ["Монтаж 6 роликов POV август", "", "", False, "", 6],
        ["", "1я половина", 15000, True, "Участник А", ""],
        ["Монтаж 6 роликов POV сентябрь", "", "", False, "", 0],
    ]
    assert _pov_block_rows(rows)[-1] == {
        "row": 3, "title": "Монтаж 6 роликов POV сентябрь", "counter": 0
    }
    assert _next_title("Монтаж 6 роликов POV сентябрь", datetime(2026, 10, 2)) == (
        "Монтаж 6 роликов POV октябрь"
    )
    assert _next_title("Монтаж 6 роликов POV октябрь", datetime(2026, 10, 2)) == (
        "Монтаж 6 роликов POV октябрь_2"
    )


def test_tool_surface_contains_tm024_read_and_write_actions():
    assert "update_pov_accounting_projection" in Operation.__args__
    assert "update_pov_accounting_projection" in MUTATION_NAMES
    assert "update_pov_accounting_projection" in WRITE_NAMES
    assert "get_pov_accounting_status" in READ_NAMES
