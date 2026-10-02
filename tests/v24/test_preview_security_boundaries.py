"""Early denial branches only; PostgreSQL locking is tested separately."""
from datetime import datetime, timedelta, timezone

import pytest
from tm_api.v24.common import Rejected
from tm_api.v24.engine import Engine


def boundary_engine(row):
    engine = Engine.__new__(Engine)
    engine.transaction_lock = lambda tx, write: None
    engine.get_preview = lambda tx, ident: row

    def forbidden(*args, **kwargs):
        pytest.fail("Denied or already-applied preview reached business effects")

    engine._key_lock = forbidden
    engine.plan = forbidden
    engine.planner = forbidden
    return engine


def preview_row(**overrides):
    return {"preview_digest": "a" * 64, "status": "preview",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "result": {"applied": True, "receipt": "synthetic-receipt"}, **overrides}


@pytest.mark.parametrize("status", ["preview", "applied"])
def test_wrong_digest_denied_before_effects_even_for_applied_preview(status):
    engine = boundary_engine(preview_row(status=status))
    with pytest.raises(Rejected, match="preview_digest_mismatch"):
        engine.apply_in_transaction(object(), "synthetic-preview", "b" * 64, "synthetic-confirmation")


def test_expired_preview_denied_before_business_effects():
    engine = boundary_engine(preview_row(
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
    with pytest.raises(Rejected, match="preview_expired"):
        engine.apply_in_transaction(object(), "synthetic-preview", "a" * 64, "synthetic-confirmation")


@pytest.mark.parametrize("status", ["cancelled", "rejected", "unknown"])
def test_non_preview_state_denied_before_business_effects(status):
    engine = boundary_engine(preview_row(status=status))
    with pytest.raises(Rejected, match="preview_expired"):
        engine.apply_in_transaction(object(), "synthetic-preview", "a" * 64, "synthetic-confirmation")


@pytest.mark.parametrize("confirmation", [None, "", "short"])
def test_invalid_confirmation_denied_before_business_effects(confirmation):
    engine = boundary_engine(preview_row())
    with pytest.raises(Rejected, match="confirmation_required"):
        engine.apply_in_transaction(object(), "synthetic-preview", "a" * 64, confirmation)


def test_applied_preview_returns_original_result_without_new_effects():
    # Expiry blocks new effects, but must not destroy an already committed receipt.
    row = preview_row(status="applied",
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    assert boundary_engine(row).apply_in_transaction(
        object(), "synthetic-preview", "a" * 64, "synthetic-confirmation") is row["result"]
