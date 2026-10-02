import pytest

from tm_api.postproduction_lifecycle import transition
from tm_api.v24.common import Rejected
from tm_api.v24.models import Mutation, Operation
from tm_api.v24.register import MUTATION_NAMES


def test_confirmed_assignment_waits_for_materials_without_reminder():
    value = transition(
        "planned",
        "assignment_confirmed",
        blocker="Ждём исходники",
    )
    assert value["state"] == "waiting_materials"
    assert value["reminder_required"] is False
    assert value["next_action"] is None


def test_materials_arrival_activates_concrete_action():
    value = transition(
        "waiting_materials",
        "materials_received",
        next_action="Скачать исходники — Бодибилдинг",
    )
    assert value["state"] == "active"
    assert value["reminder_required"] is True
    assert value["next_action"] == "Скачать исходники — Бодибилдинг"


def test_default_delivery_waits_for_feedback():
    value = transition(
        "active",
        "result_sent",
        completion_policy="explicit_approval",
    )
    assert value["state"] == "waiting_feedback"
    assert value["reminder_required"] is False


def test_kontur_delivery_auto_closes_and_late_corrections_reopen():
    delivered = transition(
        "active",
        "result_sent",
        completion_policy="delivery_auto_close_reopen_on_corrections",
    )
    assert delivered["state"] == "completed"
    assert delivered["reminder_required"] is False

    reopened = transition(
        "completed",
        "corrections_received",
        completion_policy="delivery_auto_close_reopen_on_corrections",
        next_action="Внести правки по цвету — Демо Клиент",
    )
    assert reopened["state"] == "active"
    assert reopened["reminder_required"] is True


def test_materials_received_needs_action():
    with pytest.raises(Rejected, match="materials_received_requires_next_action"):
        transition("waiting_materials", "materials_received")


def test_archive_project_is_guarded_mutation_surface():
    assert "archive_project" in Operation.__args__
    assert "archive_project" in MUTATION_NAMES
    value = Mutation.model_validate({
        "operation": "archive_project",
        "target_id": "46",
        "expected_revision": "2026-09-29T06:18:21+00:00",
        "changes": {"reason": "Ошибочная отдельная финансовая позиция"},
        "evidence": [{"kind": "user", "statement": "Убрать ошибочную позицию", "confirmation_ref": "synthetic-tm029"}],
    })
    assert value.operation == "archive_project"
