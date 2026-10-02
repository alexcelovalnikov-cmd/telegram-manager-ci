from pathlib import Path
from tm_api.v24.models import Mutation


def test_retry_attachment_is_a_guarded_universal_mutation():
    m=Mutation.model_validate({
        'operation':'retry_attachment',
        'target_id':'160',
        'expected_revision':'0123456789abcdef',
        'changes':{'reason':'reprocess historical PDF'},
        'evidence':[{'kind':'user','statement':'Retry this attachment','confirmation_ref':'retry-media-160'}],
    })
    assert m.operation=='retry_attachment'


def test_media_retry_uses_existing_database_primitive_and_scope():
    text=Path('tm_api/v25/media.py').read_text()
    assert 'tm_retry_attachment_v10' in text
    assert 'CHAT_SCOPE' in text
    assert 'attachment_source_unavailable' in text
    assert "'retry_requested'" in text


def test_contract_documents_temporary_originals():
    text=Path('tm_api/service.py').read_text()
    assert 'media_retry' in text
    assert 'does not persist original binaries' in text
