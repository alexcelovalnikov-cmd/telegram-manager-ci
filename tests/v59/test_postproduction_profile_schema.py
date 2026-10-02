import pytest
from pydantic import ValidationError

from tm_api.v25.configuration import Workspace


def test_postproduction_profile_is_valid_workspace_analysis():
    workspace = Workspace.model_validate({
        "key": "postproduction",
        "name": "Postproduction",
        "analysis": {
            "profile": "postproduction",
            "instructions": "Route postproduction work here.",
            "tracked_kinds": ["color", "editing", "cleanup"],
        },
    })
    assert workspace.analysis["profile"] == "postproduction"


def test_unknown_analysis_profile_is_rejected():
    with pytest.raises(ValidationError):
        Workspace.model_validate({
            "key": "bad-profile",
            "name": "Bad profile",
            "analysis": {"profile": "unknown-profile"},
        })
