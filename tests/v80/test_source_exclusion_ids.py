from pathlib import Path

from tm_api.v25.reading import ConfigReader


class Tx:
    def all(self, query, args):
        return [
            {'body': {'decision_key':'source_selection.excluded_names','value':[' Alice ','БОБ',' Даша   КолташЁва ']}},
            {'body': {'decision_key':'source_selection.excluded_chat_ids','value':[123,'-456',True,'bad',2.5]}},
            {'body': {'decision_key':'other.rule','value':[999]}},
        ]


def test_source_exclusions_support_stable_chat_ids_and_names():
    names, chat_ids = ConfigReader(None,'instance')._source_exclusions(Tx())
    assert names == {'alice','боб','даша колташева'}
    assert chat_ids == {123,-456}


def test_both_catalog_surfaces_apply_id_exclusions():
    text=Path('tm_api/v25/reading.py').read_text()
    assert text.count('excluded_names,excluded_ids=self._source_exclusions(tx)') == 2
    assert "int(r.get('id')) not in excluded_ids" in text
    assert "int(x.get('chat_id')) not in excluded_ids" in text


def test_contract_documents_stable_id_exclusion():
    text=Path('tm_api/service.py').read_text()
    assert 'source_selection.excluded_chat_ids' in text
    assert 'not silently deleted' in text
