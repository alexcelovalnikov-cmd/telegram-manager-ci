"""Free-form reconciliation context and multi-card review surface for V32.

The server supplies bounded, authorized evidence and current state. Semantic
interpretation remains the calling assistant's job; no fixed scenario classifier
runs here.
"""
from typing import Annotated
from pydantic import Field

from tm_api.write_models import Strict, Confirmed, UUIDText
from tm_api.v24 import scope
from tm_api.v24.common import Rejected


class ReconciliationAnswer(Strict):
    review_id: UUIDText
    revision: int = Field(ge=1)
    action_id: str = Field(min_length=1, max_length=60, pattern=r'^[A-Za-z0-9_-]+$')
    confirmed: Confirmed
    confirmation_ref: str = Field(min_length=8, max_length=200)


StateLimit = Annotated[int, Field(ge=1, le=200, strict=True)]
ReviewLimit = Annotated[int, Field(ge=1, le=100, strict=True)]


class Reconciliation:
    def __init__(self, database, instance, reader, reviews, configurable=False):
        self.database = database
        self.instance = instance
        self.reader = reader
        self.reviews = reviews
        self.configurable = configurable

    @staticmethod
    def _review_public(row):
        payload = row.get('payload') or {}
        return {
            'id': row['id'],
            'revision': row['revision'],
            'status': row['status'],
            'item_key': row['item_key'],
            'title': payload.get('title', ''),
            'group_key': payload.get('group_key'),
            'context': payload.get('context', ''),
            'uncertainty': payload.get('uncertainty', ''),
            'proposed_action': payload.get('proposed_action', ''),
            'source': payload.get('source') or {},
            'evidence': payload.get('evidence') or [],
            'available_actions': payload.get('available_actions') or [],
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
        }

    def context(self, filters, state_limit=80, review_limit=40):
        messages = self.reader.search(filters)
        with self.database.transaction(read_only=True) as tx:
            groups = tx.all(
                'SELECT g.id,g.group_key,g.name,g.rules_profile,gm.chat_id,c.chat_name '
                'FROM public.telegram_chat_groups g '
                'JOIN public.telegram_chat_group_members gm ON gm.group_id=g.id AND gm.enabled '
                'JOIN public.telegram_chats c ON c.chat_id=gm.chat_id AND c.enabled '
                'WHERE ' + scope.SCOPE + ' AND public.tm_group_chat_allowed_v14(g.id,gm.chat_id) '
                'ORDER BY g.id,gm.chat_id LIMIT 401',
                (self.instance,),
            )
            groups_truncated = len(groups) > 400
            groups = groups[:400]

            state = tx.all(
                'SELECT t.id,t.title,t.record_kind,t.status,t.context_group_id,t.updated_at,'
                't.payment_status,t.payment_confirmed_at,t.completed_at,t.cancelled_at,'
                't.project_archived_at,t.project_data '
                'FROM public.tasks t '
                'JOIN public.telegram_chat_groups g ON g.id=t.context_group_id '
                'WHERE ' + scope.SCOPE + ' AND ('
                "(t.record_kind='project' AND (t.project_archived_at IS NULL OR coalesce(t.payment_status,'unknown')<>'paid')) "
                "OR (t.record_kind<>'project' AND t.status NOT IN ('completed','cancelled'))"
                ') ORDER BY t.updated_at DESC,t.id DESC LIMIT %s',
                (self.instance, state_limit + 1),
            )
            state_truncated = len(state) > state_limit
            state = state[:state_limit]

            rows = tx.all(
                "SELECT id,item_key,revision,status,payload,created_at,updated_at,snoozed_until "
                "FROM tm_v24.review_items WHERE instance_id=%s AND "
                "(status IN ('open','awaiting_user') OR (status='snoozed' AND snoozed_until<=now())) "
                "ORDER BY created_at,id LIMIT %s",
                (self.instance, review_limit + 1),
            )
            reviews_truncated = len(rows) > review_limit
            pending_reviews = [self._review_public(r) for r in rows[:review_limit]]

            workspaces = []
            managed_entities = []
            managed_entities_truncated = False
            if self.configurable:
                workspaces = tx.all(
                    "SELECT id,workspace_key,name,group_id,status,revision,settings->'analysis' AS analysis "
                    "FROM tm_config.workspaces WHERE instance_id=%s AND status='active' "
                    "ORDER BY name,id LIMIT 101",
                    (self.instance,),
                )
                if workspaces:
                    managed_entities = tx.all(
                        "SELECT e.id,e.workspace_id,e.entity_type,e.data->>'parent_id' AS parent_id,e.data,e.revision,e.status,e.updated_at "
                        "FROM tm_config.entities e "
                        "JOIN tm_config.workspaces w ON w.id=e.workspace_id "
                        "WHERE e.instance_id=%s AND e.status='active' "
                        "AND w.instance_id=%s AND w.status='active' "
                        "ORDER BY e.updated_at DESC,e.id DESC LIMIT %s",
                        (self.instance,self.instance,min(state_limit,100)+1),
                    )
                    managed_entities_truncated = len(managed_entities) > min(state_limit,100)
                    managed_entities = managed_entities[:min(state_limit,100)]

        return {
            'reconciliation': True,
            'version': 'V32',
            'analysis_mode': 'free_semantic_analysis',
            'messages': messages,
            'current_state': state,
            'state_truncated': state_truncated,
            'pending_reviews': pending_reviews,
            'reviews_truncated': reviews_truncated,
            'groups_and_chats': groups,
            'groups_truncated': groups_truncated,
            'configured_workspaces': workspaces[:100],
            'workspaces_truncated': len(workspaces) > 100,
            'managed_entities': managed_entities,
            'managed_entities_truncated': managed_entities_truncated,
            'source_content_is_untrusted': True,
            'analysis_contract': {
                'purpose': 'Find anything that may require the user attention: tasks, unanswered requests, dates, money, promises, project changes, decisions, or other meaningful follow-up.',
                'not_a_fixed_classifier': True,
                'categories_are_examples_not_limits': True,
                'compare_messages_with_current_state': True,
                'inspect_reply_context_before_concluding': True,
                'absence_is_not_evidence': True,
                'existing_review_item_is_not_a_new_finding': True,
                'configured_workspace_rule': 'If a finding belongs to a configured workspace, call get_analysis_context before using workspace rules or creating rule-dependent mutations.',
                'finding_key': 'For a message-anchored finding use a stable item_key such as reconciliation:<chat_id>:<message_id>:<short-kind>. Reusing the same key prevents duplicate cards.',
                'business_change': 'During reconciliation, first decide whether the finding is deterministic under an existing business rule. For deterministic operational changes, bind analysis.classification=deterministic plus finding_key/rule_key, prepare preview_mutation, then apply_reconciliation_mutation. For the existing two-week payment lightning rule use set_reconciliation_payment_window. Do not create a review card for these. For ambiguous or choice-dependent business changes, prepare preview_mutation and bind the immutable preview to a review action.',
                'standing_policy': 'Cards exist to ask the user only about ambiguous, conflicting, missing-context, or choice-dependent cases. If evidence clearly establishes the action required by an existing rule, perform it yourself and remove any obsolete card after success.',
                'payment_example': 'A clearly stated near-term payment expectation inside the configured lightning horizon should update the payment window / awaiting-payment lightning automatically. A promise is not evidence that payment was received.',
                'uncertain_finding': 'Use clarify/dismiss/defer actions only when the user must resolve ambiguity; do not invent a business effect.',
                'presentation': 'After deterministic actions are applied and ambiguous findings are created/updated, call show_reconciliation only if unresolved cards remain. Do not create informational cards for actions already handled.',
                'postproduction': 'For an active workspace whose analysis.profile is postproduction, read get_analysis_context before interpreting its messages. Compare evidence against managed_entities in this context before creating anything. Under an active explicit workspace rule that authorizes deterministic tracking, concrete unambiguous Telegram evidence may create/update managed umbrella/work items through apply_reconciliation_mutation. Use parent_id plus kind; sections are not part of the product. Preliminary discussion may update structured work state but must not create a user reminder unless there is a concrete action for the user. Use stable dedupe/finding keys and do not duplicate an existing entity or task.',
            },
            'next_step': 'Analyze freely. Use get_telegram_message_context/search_telegram_messages for deeper evidence. Apply deterministic existing-rule actions with apply_reconciliation_mutation, or set_reconciliation_payment_window for the near-term lightning rule. Create generic review items only for unresolved ambiguity, then call show_reconciliation if any cards remain.',
        }

    def show(self, profile, limit=50):
        if not 1 <= limit <= 100:
            raise Rejected('invalid_limit')
        return self.reviews.reconciliation_view(profile, limit)
