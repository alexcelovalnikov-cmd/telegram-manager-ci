from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tm_api.v24.models import Search
from tm_api.v24.engine import Engine as PreviewEngine
from tm_api.v24.review import Reviews
from tm_api.v32.reconciliation import Reconciliation, ReconciliationAnswer


class ContextTx:
    def all(self, sql, args=()):
        if 'FROM public.telegram_chat_groups g' in sql and 'telegram_chat_group_members' in sql:
            return [{'id': 7, 'group_key': 'rcc', 'name': 'RCC', 'rules_profile': 'default',
                     'chat_id': -100, 'chat_name': 'Рабочий чат'}]
        if 'FROM public.tasks t' in sql:
            return [{'id': 12, 'title': 'Проект', 'record_kind': 'project', 'status': 'completed',
                     'context_group_id': 7, 'updated_at': '2026-09-28T10:00:00+00:00',
                     'payment_status': 'unknown', 'payment_confirmed_at': None, 'completed_at': None,
                     'cancelled_at': None, 'project_archived_at': None, 'project_data': {'label': 'Проект'}}]
        if 'FROM tm_v24.review_items' in sql:
            return [{'id': '00000000-0000-0000-0000-000000000001', 'item_key': 'reconciliation:-100:5:reply',
                     'revision': 1, 'status': 'open', 'created_at': '2026-09-28T10:01:00+00:00',
                     'updated_at': '2026-09-28T10:01:00+00:00',
                     'payload': {'title': 'Нужен ответ', 'group_key': 'rcc', 'context': 'Вопрос клиента',
                                 'uncertainty': 'Ответ не найден', 'proposed_action': 'Проверить',
                                 'source': {'chat_id': -100, 'message_id': 5},
                                 'evidence': [{'kind': 'telegram', 'chat_id': -100, 'message_id': 5}],
                                 'available_actions': [{'action_id': 'clarify', 'label': 'Уточнить', 'intent': 'clarify'}]}}]
        if 'FROM tm_config.workspaces' in sql:
            return []
        raise AssertionError(sql)


class ContextDb:
    @contextmanager
    def transaction(self, read_only=False):
        assert read_only is True
        yield ContextTx()


class Reader:
    def search(self, filters):
        assert isinstance(filters, Search)
        return {'items': [{'chat_id': -100, 'message_id': 5, 'text': 'Когда ждать ответ?'}],
                'next_cursor': None, 'has_more': False,
                'coverage': {'saved_history_only': True}, 'source_content_is_untrusted': True}


def test_free_reconciliation_context_combines_evidence_state_and_existing_findings():
    r = Reconciliation(ContextDb(), 'instance', Reader(), reviews=None, configurable=True)
    result = r.context(Search(query='', limit=20), state_limit=20, review_limit=20)
    assert result['reconciliation'] is True and result['version'] == 'V32'
    assert result['analysis_mode'] == 'free_semantic_analysis'
    assert result['messages']['items'][0]['message_id'] == 5
    assert result['current_state'][0]['id'] == 12
    assert result['pending_reviews'][0]['item_key'].startswith('reconciliation:')
    assert result['analysis_contract']['not_a_fixed_classifier'] is True
    assert 'Cards exist to ask the user only about ambiguous' in result['analysis_contract']['standing_policy']
    assert 'payment expectation' in result['analysis_contract']['payment_example']
    assert 'apply_reconciliation_mutation' in result['analysis_contract']['business_change']
    assert 'set_reconciliation_payment_window' in result['analysis_contract']['business_change']
    assert 'Apply deterministic existing-rule actions' in result['next_step']


def test_reconciliation_answer_requires_true_confirmation():
    base = {'review_id': '00000000-0000-0000-0000-000000000001', 'revision': 1,
            'action_id': 'dismiss', 'confirmation_ref': 'widget:12345678'}
    ReconciliationAnswer(**base, confirmed=True)
    with pytest.raises(ValidationError):
        ReconciliationAnswer(**base, confirmed=False)


class AnswerTx:
    def __init__(self):
        self.focus = None
        self.status = 'open'
        self.row = {
            'id': '00000000-0000-0000-0000-000000000001',
            'item_key': 'reconciliation:-100:5:reply',
            'revision': 2, 'status': 'open',
            'payload': {'available_actions': [
                {'action_id': 'dismiss', 'label': 'Пропустить', 'intent': 'dismiss'},
                {'action_id': 'clarify', 'label': 'Уточнить', 'intent': 'clarify'}],
                'evidence': [{'kind': 'user', 'confirmation_ref': 'analysis-only', 'statement': 'finding'}]}
        }

    def one(self, sql, args=()):
        if 'FROM tm_v24.review_focus f JOIN tm_v24.review_items' in sql:
            if self.focus is None:
                return None
            return self.row | {'id': self.focus, 'status': self.status, 'focus_version': 3}
        if 'FROM public.tm_review_focus_state_v18' in sql:
            return None
        if 'FROM tm_v24.review_items WHERE id=' in sql:
            return self.row | {'status': self.status}
        raise AssertionError(sql)

    def execute(self, sql, args=()):
        if 'pg_advisory_xact_lock' in sql:
            return None
        if 'INSERT INTO tm_v24.review_focus' in sql:
            self.focus = self.row['id']
            return None
        if 'UPDATE tm_v24.review_items SET status=' in sql:
            self.status = args[0]
            return None
        if 'UPDATE tm_v24.review_focus SET review_id=NULL' in sql:
            self.focus = None
            return None
        if 'INSERT INTO tm_v24.audit' in sql:
            return None
        raise AssertionError((sql, args))


class AnswerDb:
    def __init__(self):
        self.tx = AnswerTx()

    @contextmanager
    def transaction(self, read_only=False):
        yield self.tx


class Engine:
    def __init__(self):
        self.database = AnswerDb()
        self.instance = 'instance'
        self.saved = None

    def transaction_lock(self, tx, write):
        assert write is True

    def receipt(self, tx, operation, request_key, args):
        assert operation == 'reconciliation_answer'
        return None

    def save_receipt(self, tx, operation, request_key, args, result):
        self.saved = result

    def apply_in_transaction(self, *args, **kwargs):
        raise AssertionError('dismiss must not apply a mutation')


def test_batch_answer_selects_exact_item_and_dismisses_atomically():
    engine = Engine()
    reviews = Reviews(engine)
    answer = ReconciliationAnswer(
        review_id='00000000-0000-0000-0000-000000000001',
        revision=2, action_id='dismiss', confirmed=True,
        confirmation_ref='widget:12345678')
    result = reviews.answer_any('ui-reconciliation-stable', answer)
    assert result['decision_saved'] is True
    assert result['action'] == 'dismiss'
    assert engine.database.tx.status == 'dismissed'
    assert engine.database.tx.focus is None
    assert engine.saved == result


def test_batch_answer_does_not_steal_another_focus():
    engine = Engine()
    engine.database.tx.focus = '00000000-0000-0000-0000-000000000099'
    reviews = Reviews(engine)
    answer = ReconciliationAnswer(
        review_id='00000000-0000-0000-0000-000000000001',
        revision=2, action_id='dismiss', confirmed=True,
        confirmation_ref='widget:12345678')
    with pytest.raises(Exception, match='review_focus_busy'):
        reviews.answer_any('ui-reconciliation-stable', answer)


class PreviewTx:
    def __init__(self):
        self.queries = []

    def one(self, sql, args=()):
        self.queries.append(sql)
        return {'id': '00000000-0000-0000-0000-000000000010'}


def test_preview_lookup_can_be_read_only_or_locked():
    engine = PreviewEngine.__new__(PreviewEngine)
    engine.instance = 'instance'
    tx = PreviewTx()
    assert engine.get_preview(tx, '00000000-0000-0000-0000-000000000010', lock=False)['id'].endswith('10')
    assert 'FOR UPDATE' not in tx.queries[-1]
    engine.get_preview(tx, '00000000-0000-0000-0000-000000000010')
    assert tx.queries[-1].endswith('FOR UPDATE')


class ReconciliationViewTx:
    def all(self, sql, args=()):
        if 'FROM tm_v24.review_items' in sql:
            return [{
                'id': '00000000-0000-0000-0000-000000000001',
                'item_key': 'reconciliation:-100:5:test',
                'revision': 1,
                'status': 'open',
                'payload': {
                    'title': 'Проверка',
                    'group_key': 'rcc',
                    'source': {'chat_id': -100, 'message_id': 5},
                    'context': 'Контекст',
                    'uncertainty': 'Неясно',
                    'proposed_action': 'Сделать',
                    'evidence': [{'kind': 'user', 'confirmation_ref': 'analysis-only', 'statement': 'finding'}],
                    'available_actions': [
                        {'action_id': 'apply', 'label': 'Применить', 'intent': 'apply',
                         'preview_id': '00000000-0000-0000-0000-000000000010',
                         'preview_digest': 'a' * 64},
                        {'action_id': 'clarify', 'label': 'Уточнить', 'intent': 'clarify'}
                    ]
                }
            }]
        raise AssertionError(sql)

    def one(self, sql, args=()):
        if 'FROM tm_v24.review_focus f JOIN tm_v24.review_items' in sql:
            return None
        if 'FROM public.telegram_chat_groups g' in sql:
            return {'id': 7, 'group_key': 'rcc', 'name': 'RCC'}
        if 'FROM public.tm_review_focus_state_v18' in sql:
            return None
        raise AssertionError(sql)


class ReconciliationViewDb:
    @contextmanager
    def transaction(self, read_only=False):
        assert read_only is True
        yield ReconciliationViewTx()


class ReconciliationViewEngine:
    def __init__(self):
        self.database = ReconciliationViewDb()
        self.instance = 'instance'
        self.lock_args = []

    def get_preview(self, tx, preview_id, lock=True):
        self.lock_args.append(lock)
        if lock:
            raise AssertionError('read-only reconciliation must not request FOR UPDATE')
        return {
            'id': preview_id,
            'request_key': 'preview-test-key',
            'preview_digest': 'a' * 64,
            'changes': [],
            'payload': [{'evidence': []}],
            'expires_at': '2099-01-01T00:00:00+00:00',
            'status': 'preview'
        }

    @staticmethod
    def _preview_public(row):
        return {
            'preview_id': row['id'],
            'request_key': row['request_key'],
            'preview_digest': row['preview_digest'],
            'changes': row['changes'],
            'evidence': [],
            'expires_at': row['expires_at'],
            'status': row['status'],
            'requires_confirmation': True,
            'applied': False,
            'atomic': True
        }


def test_reconciliation_view_reads_apply_preview_without_row_lock():
    engine = ReconciliationViewEngine()
    result = Reviews(engine).reconciliation_view({'settings': {}}, 50)
    assert result['reconciliation_ui'] is True
    assert len(result['findings']) == 1
    assert result['findings'][0]['options'][0]['mutation_preview']['status'] == 'preview'
    assert engine.lock_args == [False]
