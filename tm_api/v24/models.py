"""Scenario-independent, bounded inputs; protected fields are never patches."""
from datetime import date, datetime
from typing import Annotated, Literal
from pydantic import Field, model_validator
from ..write_models import Strict, Evidence, RequestKey, UUIDText, Confirmed, Timestamp, ID

Operation = Literal[
    'create_task', 'update_task', 'complete_task', 'create_project', 'update_project', 'archive_project',
    'set_project_status', 'set_payment_state', 'update_payment',
    'create_calendar', 'update_calendar', 'create_calendar_event',
    'update_calendar_event', 'move_calendar_event', 'delete_calendar_event', 'set_calendar_member',
    'create_workspace','update_workspace','migrate_workspace_storage','archive_workspace','delete_workspace','restore_workspace',
    'create_rule','update_rule','disable_rule','activate_rule','delete_rule','restore_rule_version',
    'create_template','update_template','delete_template','create_entity_type','update_entity_type',
    'create_managed_entity','update_managed_entity','archive_managed_entity',
    'create_reminder_list','update_reminder_list','delete_reminder_list',
    'create_reminder','update_reminder','complete_reminder','delete_reminder','move_reminder','normalize_reminder_categories',
    'create_payment','set_project_cost','create_payment_expectation','update_payment_expectation',
    'retry_attachment','create_whatsapp_history_backfill_request','update_postproduction_sheet_projection',
    'update_postproduction_reminder_projection','update_pov_accounting_projection',
]

class Mutation(Strict):
    operation: Operation
    target_id: str | None = Field(default=None, max_length=128)
    group_key: str | None = Field(default=None, max_length=120)
    calendar_id: UUIDText | None = None
    expected_revision: str | int | None = None
    dedupe_key: RequestKey | None = None
    changes: dict = Field(default_factory=dict)
    evidence: Evidence
    workspace_id: UUIDText | None = None
    configuration_token: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    analysis: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def target(self):
        creating = self.operation.startswith('create_')
        if creating:
            if self.target_id is not None or self.expected_revision is not None:
                raise ValueError('Creates have no existing target/revision')
            if not self.dedupe_key:
                raise ValueError('A stable candidate dedupe_key is required')
        elif self.target_id is None or self.expected_revision is None:
            raise ValueError('An exact target and its current revision are required')
        if self.target_id is not None:
            if self.operation in ('update_task','complete_task','update_project','archive_project','set_project_status','set_payment_state','set_project_cost','retry_attachment'):
                if not self.target_id.isdigit() or not 1<=int(self.target_id)<=9223372036854775807:
                    raise ValueError('A positive existing task/project/attachment ID is required')
            elif self.operation=='set_calendar_member':
                import re
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',self.target_id):
                    raise ValueError('Use the existing calendar username')
            else:
                from uuid import UUID
                UUID(self.target_id)
        if not self.changes and self.operation not in ('complete_task','delete_calendar_event','archive_workspace','delete_workspace','restore_workspace','disable_rule','activate_rule','delete_rule','delete_template','archive_managed_entity','delete_reminder_list','complete_reminder','delete_reminder','move_calendar_event'):
            raise ValueError('An empty mutation has no business effect')
        return self

class Search(Strict):
    query: str = Field(default='', max_length=500)
    mode: Literal['websearch', 'literal'] = 'websearch'
    date_from: Timestamp | None = None
    date_to: Timestamp | None = None
    chat_ids: list[int] | None = Field(default=None, max_length=200)
    group_key: str | None = Field(default=None, max_length=120)
    project_id: ID | None = None
    sender_id: int | None = None
    limit: int = Field(default=40, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    include_attachments: bool = True
    include_context: bool = False

    @model_validator(mode='after')
    def dates(self):
        if self.date_from and self.date_to and datetime.fromisoformat(self.date_from.replace('Z','+00:00')) >= datetime.fromisoformat(self.date_to.replace('Z','+00:00')):
            raise ValueError('date_to is exclusive and must be after date_from')
        if self.chat_ids is not None and not self.chat_ids:
            raise ValueError('Use null for all permitted chats, not an empty list')
        return self


class WhatsAppSearch(Strict):
    query: str = Field(default='', max_length=500)
    date_from: Timestamp | None = None
    date_to: Timestamp | None = None
    chat_ids: list[str] | None = Field(default=None, max_length=100)
    sender_jid: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=40, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    include_context: bool = True

    @model_validator(mode='after')
    def bounded(self):
        if self.date_from and self.date_to and datetime.fromisoformat(self.date_from.replace('Z','+00:00')) >= datetime.fromisoformat(self.date_to.replace('Z','+00:00')):
            raise ValueError('date_to is exclusive and must be after date_from')
        if self.chat_ids is not None:
            if not self.chat_ids:
                raise ValueError('Use null for all WhatsApp chats')
            if any(not isinstance(x,str) or not x or len(x)>200 for x in self.chat_ids):
                raise ValueError('Invalid WhatsApp chat JID')
        return self

class ReviewAction(Strict):
    action_id: str = Field(min_length=1, max_length=60, pattern=r'^[A-Za-z0-9_-]+$')
    label: str = Field(min_length=1, max_length=160)
    intent: Literal['apply', 'clarify', 'dismiss', 'defer']
    preview_id: UUIDText | None = None
    preview_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    defer_until: Timestamp | None = None

    @model_validator(mode='after')
    def binding(self):
        if self.intent == 'apply':
            if not self.preview_id or not self.preview_digest:
                raise ValueError('A business button requires an immutable preview')
        elif self.preview_id or self.preview_digest:
            raise ValueError('Only apply can refer to a mutation')
        if (self.intent == 'defer') != (self.defer_until is not None):
            raise ValueError('A defer action needs an explicit future date')
        return self

class ReviewDraft(Strict):
    item_key: RequestKey
    group_key: str | None = Field(default=None, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    source: dict = Field(default_factory=dict)
    context: str = Field(max_length=12000)
    uncertainty: str = Field(max_length=4000)
    proposed_action: str = Field(default='', max_length=1000)
    evidence: Evidence
    available_actions: list[ReviewAction] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def distinct_actions(self):
        ids = [a.action_id for a in self.available_actions]
        if len(set(ids)) != len(ids):
            raise ValueError('Action IDs must be unique')
        if not any(a.intent == 'clarify' for a in self.available_actions):
            raise ValueError('Keep a clarification action')
        return self

class ReviewAnswer(Strict):
    review_id: UUIDText
    revision: int = Field(ge=1)
    focus_version: int = Field(ge=1)
    action_id: str = Field(min_length=1, max_length=60)
    confirmed: Confirmed
    confirmation_ref: str = Field(min_length=8, max_length=200)

# Calendar dates are ISO strings: all-day dates are not midnight timestamps.
class Event(Strict):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default='', max_length=20000)
    location: str = Field(default='', max_length=2000)
    start_at: str = Field(max_length=64)
    end_at: str = Field(max_length=64)
    timezone: str = Field(default='Asia/Yekaterinburg', max_length=120)
    all_day: bool = False
    recurrence: str | None = Field(default=None, max_length=1000)
    status: Literal['confirmed', 'tentative', 'cancelled'] = 'confirmed'

    @model_validator(mode='after')
    def validate_times(self):
        from zoneinfo import ZoneInfo
        try: ZoneInfo(self.timezone)
        except (ValueError,KeyError): raise ValueError("Unknown IANA timezone") from None
        if self.all_day:
            a, z = date.fromisoformat(self.start_at), date.fromisoformat(self.end_at)
        else:
            a, z = datetime.fromisoformat(self.start_at.replace('Z','+00:00')), datetime.fromisoformat(self.end_at.replace('Z','+00:00'))
            if a.tzinfo is None or z.tzinfo is None:
                raise ValueError('Use explicit offsets for timed events')
        if z <= a:
            raise ValueError('end_at must be after start_at (exclusive for all-day)')
        if self.recurrence and ('\n' in self.recurrence or '\r' in self.recurrence):
            raise ValueError('A recurrence is a single RRULE value')
        return self
