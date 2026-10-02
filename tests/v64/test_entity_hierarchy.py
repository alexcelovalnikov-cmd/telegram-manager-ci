import pytest
from tm_api.v25.configuration import EntityType, validate_entity
from tm_api.v24.common import Rejected


def test_parent_is_reserved_metadata():
    definition={"label":"Child","fields":{"kind":{"type":"text"}},"required":["kind"],"states":["todo"]}
    data=validate_entity(definition,{
        "title":"Color pass","state":"todo","kind":"color",
        "parent_id":"11111111-1111-4111-8111-111111111111",
    })
    assert data["parent_id"]=="11111111-1111-4111-8111-111111111111"


def test_section_is_no_longer_supported():
    definition={"label":"Child","fields":{"kind":{"type":"text"}},"required":["kind"],"states":["todo"]}
    with pytest.raises(Rejected):
        validate_entity(definition,{"title":"Color pass","state":"todo","kind":"color","section":"Color"})


def test_entity_type_cannot_redefine_parent_id():
    with pytest.raises(Exception):
        EntityType.model_validate({"label":"Bad","fields":{"parent_id":{"type":"text"}}})
