"""Prepare a proposal, never an authorization or a direct project write."""
from .backend import GuardRejected
from .project_model.tm_projects import parse_title
from .project_model.tm_finance import amount_breakdown

async def prepare(service, request_key, change):
    payload=change.model_dump(mode='json')
    patch=change.patch.model_dump(exclude_unset=True)
    if change.evidence_assessment=='ambiguous':
        # No proposal/focus/business mutation for ambiguous evidence.
        await service.get_review_sources(change.review_id,change.review_revision,change.expected_version)
        return {'prepared':False,'needs_clarification':True,'instruction':'Keep this question active; describe the ambiguity and ask the user. Do not present the hypothesis as fact.'}
    expression=patch.pop('amount_expression',None)
    if expression is not None:
        parsed=amount_breakdown('Project - '+expression)
        if not parsed['complete']:
            return {'prepared':False,'needs_clarification':True,'instruction':'The component expression is incomplete or ambiguous. Keep the same focus and clarify; no proposal was created.'}
        patch.update(amount_rub=parsed['total'],amount_breakdown=parsed,amount_status='stated')
    payload['patch']=patch
    return await service.guarded_write('prepare_review_project_change',request_key,payload)
