"""Closed input contracts for guarded V19 operations."""
from datetime import datetime
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_validator

ID = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
Version = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
RequestKey = Annotated[str, Field(min_length=16, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]
UUIDText = Annotated[str, Field(pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]

def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return value
Timestamp = Annotated[str, Field(min_length=20, max_length=40), AfterValidator(timestamp)]
Money = Annotated[str, Field(pattern=r'^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?$')]
Text = Annotated[str, Field(min_length=2, max_length=4000)]

def confirmed(value):
    if value is not True:
        raise ValueError('Explicit confirmation required')
    return value
Confirmed = Annotated[bool, Field(strict=True), AfterValidator(confirmed)]

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class UserEvidence(Strict):
    kind: Literal['user']
    confirmation_ref: Annotated[str, Field(min_length=8, max_length=200)]
    statement: Text

class TelegramEvidence(Strict):
    kind: Literal['telegram']
    chat_id: Annotated[int, Field(strict=True, ge=-9223372036854775808, le=9223372036854775807)]
    message_id: ID
    content_token: Annotated[str, Field(min_length=1, max_length=256)]

Evidence = Annotated[list[Annotated[Union[UserEvidence, TelegramEvidence], Field(discriminator='kind')]], Field(min_length=1, max_length=30)]

class RelativeWindow(Strict):
    kind: Literal['relative']
    within_days: Annotated[int, Field(strict=True, ge=0, le=14)]

class DatedWindow(Strict):
    kind: Literal['exact', 'range']
    start_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    end_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    exact_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None

class PaymentWindow(Strict):
    project_id: ID
    expected_updated_at: Timestamp
    window: Annotated[Union[RelativeWindow, DatedWindow], Field(discriminator='kind')] | None
    evidence: Evidence
    confirmed: Confirmed

class ReconciliationPaymentWindow(Strict):
    """Standing-policy payment expectation update used only during reconciliation."""
    project_id: ID
    expected_updated_at: Timestamp
    window: Annotated[Union[RelativeWindow, DatedWindow], Field(discriminator='kind')] | None
    evidence: Annotated[list[TelegramEvidence], Field(min_length=1, max_length=30)]
    finding_key: RequestKey
    rule_key: Literal['payment_lightning_two_week_rule']
    rationale: Text

class UpdateProject(Strict):
    project_id: ID
    expected_updated_at: Timestamp
    proposal_id: UUIDText
    expected_proposal_updated_at: Timestamp
    evidence: Evidence
    confirmed: Confirmed

class PaymentEvent(Strict):
    kind: Literal['invoiced', 'promised', 'queued', 'available', 'received', 'refund']
    amount: Money
    currency: Literal['RUB'] = 'RUB'
    method: Annotated[str, Field(min_length=1, max_length=80)] = 'unknown'
    counterparty: Annotated[str, Field(max_length=200)] | None = None
    external_ref: Annotated[str, Field(max_length=200)] | None = None
    occurred_at: Timestamp
    state: Literal['confirmed'] = 'confirmed'

class Allocation(Strict):
    task_id: ID
    expected_updated_at: Timestamp
    work_key: Annotated[str, Field(min_length=1, max_length=120)] = 'main'
    amount: Money

class RecordPayment(Strict):
    group_key: Annotated[str, Field(min_length=1, max_length=120)]
    event: PaymentEvent
    allocations: Annotated[list[Allocation], Field(min_length=1, max_length=100)]
    evidence: Evidence
    confirmed: Confirmed

class CreateProjectRequest(Strict):
    proposal_id: UUIDText
    expected_proposal_updated_at: Timestamp
    evidence: Evidence
    confirmed: Confirmed

class RequestState(Strict):
    project_id: ID
    expected_updated_at: Timestamp
    state: Literal['pending', 'confirmed', 'rejected']
    evidence: Evidence
    confirmed: Confirmed

class RequestEffect(Strict):
    operation: Literal['set_project_request_state']
    arguments: RequestState

class WindowEffect(Strict):
    operation: Literal['set_payment_window']
    arguments: PaymentWindow

class UpdateEffect(Strict):
    operation: Literal['update_existing_project']
    arguments: UpdateProject

class Answer(Strict):
    review_id: UUIDText
    review_revision: Revision
    expected_version: Version
    action: Literal['clarify', 'defer', 'dismiss', 'resolved']
    user_text: Text
    confirmed: Confirmed
    effect: Annotated[Union[WindowEffect, UpdateEffect, RequestEffect], Field(discriminator='operation')] | None = None

    @model_validator(mode='after')
    def atomic_resolution(self):
        if (self.action == 'resolved') != (self.effect is not None):
            raise ValueError('Resolution requires one linked business action')
        return self

class ProjectClarificationPatch(Strict):
    label: Annotated[str, Field(min_length=1, max_length=350)] | None = None
    date_mmdd: Annotated[str, Field(pattern=r'^\d{2}\.\d{2}$')] | None = None
    date_iso: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None
    amount_rub: Money | None = None
    amount_expression: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    aliases: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=30)] | None = None

    @model_validator(mode='after')
    def nonempty(self):
        fields=self.model_dump(exclude_unset=True)
        if not fields or any(v is None for v in fields.values()):
            raise ValueError('A concrete non-null change is required')
        if self.amount_expression is not None and self.amount_rub is not None:
            raise ValueError('Choose a total or a component expression')
        return self

class ReviewProjectChange(Strict):
    review_id: UUIDText
    review_revision: Revision
    expected_version: Version
    project_id: ID
    expected_updated_at: Timestamp
    patch: ProjectClarificationPatch
    evidence: Annotated[list[TelegramEvidence], Field(min_length=1, max_length=20)]
    evidence_assessment: Literal['supported','ambiguous']
    rationale: Text
