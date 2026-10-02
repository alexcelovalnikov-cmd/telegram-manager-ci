from pathlib import Path
import pytest
from tm_api.v25.configuration import validate_entity
from tm_api.v24.common import Rejected


def test_section_rejected_by_entity_validation():
    definition={"label":"Item","fields":{"kind":{"type":"text"}},"required":["kind"],"states":[]}
    with pytest.raises(Rejected):
        validate_entity(definition,{"kind":"color","section":"Color"})


def test_public_surface_no_section_filter():
    text=Path('tm_api/v25/register.py').read_text()
    assert 'section:str|None' not in text
    assert 'section groups children' not in text


def test_schema_marks_sections_unsupported():
    text=Path('tm_api/v25/reading.py').read_text()
    assert "'managed_entity_sections_supported':False" in text
