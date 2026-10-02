"""Read-only RCC canonical population proposal.

Semantic project interpretation stays with the calling assistant. This module
validates immutable Telegram evidence, legacy Sheet bindings and current
canonical state before any managed-entity write is considered.
"""
import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from tm_api.write_models import ID, Money, Strict
from tm_api.v24.common import Rejected
from tm_api.v25.configuration import validate_entity
from .settlement import PROJECT_TYPE, ITEM_TYPE, PAYMENT_TYPE
from .sheets import GoogleSheets, SheetSyncError, _number, _row10, _norm

POPULATION_CONTRACT='tm-rcc-canonical-population/v1'

ChatID=Annotated[int,Field(strict=True,ge=-9223372036854775808,le=9223372036854775807)]
SheetRow=Annotated[int,Field(strict=True,ge=2,le=1000)]
ShortText=Annotated[str,Field(min_length=1,max_length=200)]

class PopulationEvidence(Strict):
    chat_id: ChatID
    message_id: ID
    content_token: Annotated[str,Field(min_length=1,max_length=256)]
    role: Literal['project_identity','estimate','scope']
    attachment_id: Annotated[int,Field(strict=True,ge=1,le=9223372036854775807)]|None=None
class PopulationItemCandidate(Strict):
    sheet_row: SheetRow
    title: ShortText
    category: Annotated[str,Field(min_length=1,max_length=80)]='other'
    amount_rub: Money
    client_settlement: bool=True
    personal_income: bool
    pass_through: bool|None=None
    recipient: Annotated[str,Field(min_length=1,max_length=200)]|None=None
    performer: Annotated[str,Field(min_length=1,max_length=200)]|None=None
    order: Annotated[int,Field(strict=True,ge=0,le=100000)]=1000
    identity_evidence: PopulationEvidence
    evidence: Annotated[list[PopulationEvidence],Field(max_length=10)]=Field(default_factory=list)

class PopulationProjectCandidate(Strict):
    sheet_row: SheetRow
    title: ShortText
    date_iso: Annotated[str,Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]|None=None
    personal_project_id: Annotated[int,Field(strict=True,ge=1,le=9223372036854775807)]|None=None
    identity_evidence: PopulationEvidence
    evidence: Annotated[list[PopulationEvidence],Field(max_length=10)]=Field(default_factory=list)
    items: Annotated[list[PopulationItemCandidate],Field(min_length=1,max_length=30)]

def _digest(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()
    return hashlib.sha256(raw).hexdigest()

def _evidence_key(value):
    return (int(value['chat_id']),int(value['message_id']),
            int(value['attachment_id']) if value.get('attachment_id') is not None else None)
def _public_evidence(row):
    if not row:
        return None
    result={k:row.get(k) for k in ('chat_id','message_id','sender_name','date','content_token','text_preview')}
    if row.get('attachment_id') is not None:
        result['attachment']={k:row.get(k) for k in (
            'attachment_id','file_name','processing_status','attachment_summary')}
    return result

def _candidate(value,model):
    return value if isinstance(value,model) else model.model_validate(value)

def _entity_id(instance,dedupe_key):
    # Must exactly match ConfigBusiness.stable(): a later guarded
    # create_managed_entity with this dedupe_key will create this UUID.
    return str(uuid5(NAMESPACE_URL,f'tm-v25:{instance}:{dedupe_key}'))

def _excluded_title(value):
    normalized=_norm(value)
    return normalized=='готово боев' or normalized.startswith('монтаж 6 роликов pov')

def _sheet_section(values,row_index,total_index):
    current=None
    for i in range(1,min(row_index+1,total_index)):
        row=values[i]
        if str(row[0] or '').strip() and not str(row[1] or '').strip():
            current=i
    return current

def _evidence_binding(candidate,lookup,blockers,owner):
    refs=[candidate.identity_evidence,*candidate.evidence]
    public=[]
    seen=set()
    for ref in refs:
        value=ref.model_dump()
        key=_evidence_key(value)
        if key in seen:
            continue
        seen.add(key)
        current=lookup.get(key)
        if current is None:
            blockers.append({'code':'population_evidence_not_found','owner':owner,
                             'chat_id':value['chat_id'],'message_id':value['message_id'],
                             'attachment_id':value.get('attachment_id')})
            continue
        if str(current.get('content_token') or '')!=value['content_token']:
            blockers.append({'code':'population_evidence_token_changed','owner':owner,
                             'chat_id':value['chat_id'],'message_id':value['message_id']})
        public.append({'role':value['role'],'requested':value,'current':_public_evidence(current)})
    return public

def _project_data(project):
    data={'title':project.title}
    if project.date_iso is not None:
        data['date_iso']=project.date_iso
    if project.personal_project_id is not None:
        data['personal_project_id']=project.personal_project_id
    return data

def _item_data(item,parent_id):
    data={'parent_id':parent_id,'title':item.title,'category':item.category,
          'amount_rub':str(Decimal(item.amount_rub).quantize(Decimal('.01'))),
          'client_settlement':item.client_settlement,'personal_income':item.personal_income,
          'pass_through':item.pass_through if item.pass_through is not None else not item.personal_income,
          'order':item.order}
    if item.recipient is not None:
        data['recipient']=item.recipient
    if item.performer is not None:
        data['performer']=item.performer
    return data
def _canonical_rows_fingerprint(rows):
    public=[{'id':str(r['id']),'entity_type':r['entity_type'],'data':r.get('data') or {},
             'status':r.get('status'),'revision':r.get('revision')} for r in rows]
    public.sort(key=lambda x:(x['entity_type'],x['id']))
    return _digest(public)

def _canonical_indexes(rows):
    by_id={}
    projects={}
    items={}
    for row in rows:
        ident=str(row['id'])
        by_id[ident]=row
        if row.get('status')!='active':
            continue
        data=row.get('data') or {}
        if row.get('entity_type')==PROJECT_TYPE:
            projects.setdefault(_norm(data.get('title')),[]).append(row)
        elif row.get('entity_type')==ITEM_TYPE:
            key=(str(data.get('parent_id') or ''),_norm(data.get('title')))
            items.setdefault(key,[]).append(row)
    return by_id,projects,items

def _resolve_existing(entity,by_id,projects,items,blockers):
    existing=by_id.get(entity['id'])
    if existing is not None:
        if existing.get('status')!='active' or existing.get('entity_type')!=entity['entity_type']:
            blockers.append({'code':'population_existing_entity_conflict','entity_id':entity['id']})
            return 'blocked'
        if (existing.get('data') or {})!=entity['data']:
            blockers.append({'code':'population_existing_entity_data_conflict','entity_id':entity['id']})
            return 'blocked'
        entity['existing_revision']=existing.get('revision')
        return 'match_existing'
    if entity['entity_type']==PROJECT_TYPE:
        collisions=projects.get(_norm(entity['data']['title'])) or []
        if collisions:
            blockers.append({'code':'population_existing_project_title_conflict',
                             'entity_id':entity['id'],
                             'existing_ids':[str(x['id']) for x in collisions]})
            return 'blocked'
    elif entity['entity_type']==ITEM_TYPE:
        key=(entity['data']['parent_id'],_norm(entity['data']['title']))
        collisions=items.get(key) or []
        if collisions:
            blockers.append({'code':'population_existing_item_conflict',
                             'entity_id':entity['id'],
                             'existing_ids':[str(x['id']) for x in collisions]})
            return 'blocked'
    return 'create'

def _validate_schema(entity,schemas,blockers):
    if schemas is None:
        return
    body=schemas.get(entity['entity_type'])
    if body is None:
        blockers.append({'code':'population_entity_type_schema_missing',
                         'entity_type':entity['entity_type']})
        return
    try:
        validate_entity(body,entity['data'])
    except Rejected as exc:
        blockers.append({'code':'population_entity_schema_rejected',
                         'entity_id':entity['id'],'error':str(exc)})

def build_population_proposal(sheet_rows,canonical_rows,candidate_projects,evidence_rows,
                              context=None,entity_schemas=None,valid_personal_project_ids=None):
    context=dict(context or {})
    values=[_row10(row) for row in sheet_rows]
    total_index=next((i for i,row in enumerate(values) if _norm(row[0])=='итого:'),len(values))
    evidence_lookup={_evidence_key(row):row for row in evidence_rows}
    by_id,existing_projects,existing_items=_canonical_indexes(canonical_rows)
    blockers=[]
    entities=[]
    seen_ids=set()
    candidate_project_titles=set()
    candidate_item_keys=set()
    used_sheet_rows=set()
    instance=str(context.get('instance_id') or '')
    candidates=[_candidate(value,PopulationProjectCandidate) for value in candidate_projects]
    identity_chat_ids={int(x) for x in (context.get('identity_chat_ids') or [])}
    valid_personal_project_ids=(
        None if valid_personal_project_ids is None
        else {int(x) for x in valid_personal_project_ids})

    if context.get('write_mode') not in (None,'observe'):
        blockers.append({'code':'population_requires_observe_mode',
                         'write_mode':context.get('write_mode')})
    if not identity_chat_ids:
        blockers.append({'code':'population_identity_source_unresolved'})
    elif len(identity_chat_ids)!=1:
        blockers.append({'code':'population_identity_source_ambiguous',
                         'chat_ids':sorted(identity_chat_ids)})
    for project in candidates:
        pidx=project.sheet_row-1
        owner=f'project:{project.sheet_row}'
        project_title_key=_norm(project.title)
        if project_title_key in candidate_project_titles:
            blockers.append({'code':'population_duplicate_candidate_project_title',
                             'sheet_row':project.sheet_row,'title':project.title})
        candidate_project_titles.add(project_title_key)
        if project.sheet_row in used_sheet_rows:
            blockers.append({'code':'population_duplicate_candidate_sheet_binding',
                             'sheet_row':project.sheet_row})
        used_sheet_rows.add(project.sheet_row)
        if pidx>=total_index or pidx>=len(values):
            blockers.append({'code':'population_invalid_project_sheet_row','sheet_row':project.sheet_row})
            continue
        prow=values[pidx]
        legacy_project_title=str(prow[0] or '').strip()
        if not legacy_project_title or str(prow[1] or '').strip():
            blockers.append({'code':'population_project_row_not_header','sheet_row':project.sheet_row})
        if any(prow[i] not in ('',None) for i in range(6,10)):
            blockers.append({'code':'population_project_row_already_bound','sheet_row':project.sheet_row})
        if _excluded_title(legacy_project_title) or _excluded_title(project.title):
            blockers.append({'code':'population_excluded_project','sheet_row':project.sheet_row,
                             'title':legacy_project_title or project.title})
        if (project.personal_project_id is not None and
                valid_personal_project_ids is not None and
                project.personal_project_id not in valid_personal_project_ids):
            blockers.append({'code':'population_personal_project_not_found',
                             'sheet_row':project.sheet_row,
                             'personal_project_id':project.personal_project_id})
        if project.identity_evidence.role not in ('project_identity','estimate'):
            blockers.append({'code':'population_project_identity_evidence_required',
                             'sheet_row':project.sheet_row})
        if project.identity_evidence.chat_id not in identity_chat_ids:
            blockers.append({'code':'population_project_identity_not_alexandra_source',
                             'sheet_row':project.sheet_row,
                             'chat_id':project.identity_evidence.chat_id})
        pevidence=_evidence_binding(project,evidence_lookup,blockers,owner)
        anchor=project.identity_evidence.model_dump()
        pdedupe=_digest({'workspace_id':context.get('workspace_id'),
                         'identity':_evidence_key(anchor)})
        project_dedupe_key='rcc-project:'+pdedupe
        project_id=_entity_id(instance,project_dedupe_key)
        pdata=_project_data(project)
        pentity={'id':project_id,'entity_type':PROJECT_TYPE,'data':pdata,
                 'dedupe_key':project_dedupe_key,
                 'fingerprint':_digest({'entity_type':PROJECT_TYPE,'data':pdata,'dedupe':pdedupe}),
                 'binding':{'legacy_sheet_row':project.sheet_row,
                            'legacy_title':legacy_project_title,
                            'legacy_title_preserved':True,'evidence':pevidence}}
        if project_id in seen_ids:
            blockers.append({'code':'population_duplicate_candidate_entity','entity_id':project_id})
        seen_ids.add(project_id)
        _validate_schema(pentity,entity_schemas,blockers)
        pentity['action']=_resolve_existing(
            pentity,by_id,existing_projects,existing_items,blockers)
        entities.append(pentity)

        for item in project.items:
            iidx=item.sheet_row-1
            iowner=f'item:{item.sheet_row}'
            item_semantic_key=(project_id,_norm(item.title))
            if item_semantic_key in candidate_item_keys:
                blockers.append({'code':'population_duplicate_candidate_item',
                                 'sheet_row':item.sheet_row,
                                 'project_id':project_id,'title':item.title})
            candidate_item_keys.add(item_semantic_key)
            if item.sheet_row in used_sheet_rows:
                blockers.append({'code':'population_duplicate_candidate_sheet_binding',
                                 'sheet_row':item.sheet_row})
            used_sheet_rows.add(item.sheet_row)
            if iidx>=total_index or iidx>=len(values):
                blockers.append({'code':'population_invalid_item_sheet_row','sheet_row':item.sheet_row})
                continue
            irow=values[iidx]
            if _sheet_section(values,iidx,total_index)!=pidx or not str(irow[1] or '').strip():
                blockers.append({'code':'population_item_not_under_project','sheet_row':item.sheet_row,
                                 'project_sheet_row':project.sheet_row})
            if any(irow[i] not in ('',None) for i in range(6,10)):
                blockers.append({'code':'population_item_row_already_bound','sheet_row':item.sheet_row})
            sheet_amount=_number(irow[2])
            candidate_amount=Decimal(item.amount_rub)
            if sheet_amount is None or sheet_amount!=candidate_amount:
                blockers.append({'code':'population_item_amount_mismatch','sheet_row':item.sheet_row,
                                 'sheet_amount_rub':None if sheet_amount is None else str(sheet_amount),
                                 'candidate_amount_rub':str(candidate_amount)})
            ievidence=_evidence_binding(item,evidence_lookup,blockers,iowner)
            ianchor=item.identity_evidence.model_dump()
            idedupe=_digest({'project_id':project_id,'identity':_evidence_key(ianchor),
                             'legacy_title':_norm(irow[1]),'amount_rub':str(candidate_amount)})
            item_dedupe_key='rcc-item:'+idedupe
            item_id=_entity_id(instance,item_dedupe_key)
            idata=_item_data(item,project_id)
            ientity={'id':item_id,'entity_type':ITEM_TYPE,'data':idata,
                     'dedupe_key':item_dedupe_key,
                     'fingerprint':_digest({'entity_type':ITEM_TYPE,'data':idata,'dedupe':idedupe}),
                     'binding':{'legacy_sheet_row':item.sheet_row,
                                'legacy_title':str(irow[1] or '').strip(),
                                'legacy_title_preserved':True,'evidence':ievidence,
                                'sheet_paid_ignored':True,'sheet_payment_type_ignored':True,
                                'ready_column_ignored':True}}
            if item_id in seen_ids:
                blockers.append({'code':'population_duplicate_candidate_entity','entity_id':item_id})
            seen_ids.add(item_id)
            _validate_schema(ientity,entity_schemas,blockers)
            ientity['action']=_resolve_existing(
                ientity,by_id,existing_projects,existing_items,blockers)
            entities.append(ientity)
    evidence_public=sorted(
        (_public_evidence(x) for x in evidence_rows),
        key=lambda x:(x.get('chat_id'),x.get('message_id'),
                      (x.get('attachment') or {}).get('attachment_id') or 0))
    source_fingerprint=_digest(values)
    canonical_fingerprint=_canonical_rows_fingerprint(canonical_rows)
    evidence_fingerprint=_digest(evidence_public)
    candidate_fingerprint=_digest([x.model_dump() for x in candidates])
    entity_fingerprint=_digest([
        {k:e[k] for k in ('id','entity_type','data','dedupe_key','fingerprint')}
        for e in entities])
    plan_token=_digest({
        'contract_version':POPULATION_CONTRACT,'context':context,
        'source_fingerprint':source_fingerprint,
        'canonical_fingerprint':canonical_fingerprint,
        'evidence_fingerprint':evidence_fingerprint,
        'candidate_fingerprint':candidate_fingerprint,
        'entity_fingerprint':entity_fingerprint})

    return {'contract_version':POPULATION_CONTRACT,'mode':'dry_run',
            'plan_token':plan_token,'safe_to_populate':not blockers,
            'blockers':blockers,'source_fingerprint':source_fingerprint,
            'canonical_fingerprint':canonical_fingerprint,
            'evidence_fingerprint':evidence_fingerprint,
            'candidate_fingerprint':candidate_fingerprint,
            'entity_fingerprint':entity_fingerprint,
            'guards':{'read_only':True,'write_mode_unchanged':True,
                      'legacy_sheet_rows_unchanged':True,
                      'legacy_titles_preserved':True,
                      'receipt_entities_created':False,
                      'receipt_inference_forbidden':True,
                      'identity_requires_telegram_evidence':True,
                      'date_is_not_identity':True},
            'entities':entities,'sheet_mutations':[],
            'counts':{
                'projects':sum(1 for x in entities if x['entity_type']==PROJECT_TYPE),
                'items':sum(1 for x in entities if x['entity_type']==ITEM_TYPE)},
            **{k:v for k,v in context.items() if k!='instance_id'}}

def _all_evidence(candidates):
    refs=[]
    for project in candidates:
        refs.extend([project.identity_evidence,*project.evidence])
        for item in project.items:
            refs.extend([item.identity_evidence,*item.evidence])
    unique={}
    for ref in refs:
        unique[_evidence_key(ref.model_dump())]=ref
    if len(unique)>200:
        raise SheetSyncError('population_evidence_limit_exceeded')
    return list(unique.values())
def _load_evidence(tx,group_id,refs):
    result=[]
    for ref in refs:
        row=tx.one(
            "SELECT m.chat_id,m.message_id,m.sender_name,coalesce(m.date,m.created_at) AS date,"
            "public.tm_content_token_v11(m.chat_id,m.message_id) AS content_token,"
            "left(coalesce(m.text,''),800) AS text_preview "
            "FROM public.telegram_messages m "
            "JOIN public.telegram_chat_group_members gm "
            "ON gm.chat_id=m.chat_id AND gm.group_id=%s AND gm.enabled "
            "WHERE m.chat_id=%s AND m.message_id=%s AND NOT m.is_deleted "
            "AND public.tm_group_chat_allowed_v14(gm.group_id,m.chat_id)",
            (group_id,ref.chat_id,ref.message_id))
        if not row:
            continue
        row=dict(row)
        if ref.attachment_id is not None:
            attachment=tx.one(
                "SELECT a.id AS attachment_id,a.file_name,a.processing_status,"
                "left(coalesce(a.content_summary,a.extracted_text,a.transcript,''),500) "
                "AS attachment_summary FROM public.telegram_attachments a "
                "JOIN public.telegram_messages m "
                "ON m.chat_id=a.chat_id AND m.message_id=a.message_id "
                "WHERE a.id=%s AND a.chat_id=%s AND a.message_id=%s "
                "AND a.source_token=m.media_source_token AND a.deleted_at IS NULL",
                (ref.attachment_id,ref.chat_id,ref.message_id))
            if not attachment:
                continue
            row.update(attachment)
        else:
            row['attachment_id']=None
        result.append(row)
    return result
def proposal_configured(database,instance,credentials_path,workspace_id,candidate_projects):
    candidates=[_candidate(value,PopulationProjectCandidate) for value in candidate_projects]
    refs=_all_evidence(candidates)
    requested_personal_project_ids=sorted({
        int(project.personal_project_id) for project in candidates
        if project.personal_project_id is not None})
    with database.transaction(read_only=True) as tx:
        workspace=tx.one(
            "SELECT id,workspace_key,name,group_id,settings,status "
            "FROM tm_config.workspaces WHERE id=%s::uuid AND instance_id=%s",
            (workspace_id,instance))
        if not workspace or workspace.get('status')!='active':
            raise SheetSyncError('active_workspace_required')
        profile=((workspace.get('settings') or {}).get('analysis') or {}).get('profile')
        if profile!='rcc_settlement':
            raise SheetSyncError('rcc_settlement_profile_required')
        integration=(((workspace.get('settings') or {}).get('integrations') or {})
                     .get('google_sheets'))
        if not isinstance(integration,dict) or integration.get('mode')!='rcc_settlement':
            raise SheetSyncError('active_rcc_sheet_integration_required')

        canonical_rows=tx.all(
            "SELECT id,entity_type,data,status,revision FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid "
            "AND entity_type=ANY(%s::text[]) ORDER BY id",
            (instance,workspace_id,[PROJECT_TYPE,ITEM_TYPE,PAYMENT_TYPE]))
        schema_rows=tx.all(
            "SELECT document_key,body FROM tm_config.documents "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND kind='entity_type' "
            "AND status='active' AND document_key=ANY(%s::text[]) "
            "ORDER BY document_key,id",
            (instance,workspace_id,[PROJECT_TYPE,ITEM_TYPE]))
        schemas={}
        for row in schema_rows:
            if row['document_key'] in schemas:
                raise SheetSyncError('duplicate_population_entity_type_schema')
            schemas[row['document_key']]=row['body']
        evidence_rows=_load_evidence(tx,workspace.get('group_id'),refs)
        personal_project_rows=[]
        if requested_personal_project_ids:
            personal_project_rows=tx.all(
                "SELECT id FROM public.tasks WHERE id=ANY(%s::bigint[]) "
                "AND context_group_id=%s AND record_kind='project' ORDER BY id",
                (requested_personal_project_ids,workspace.get('group_id')))
        valid_personal_project_ids=[int(row['id']) for row in personal_project_rows]
        sources=tx.all(
            "SELECT gm.chat_id,c.chat_name FROM public.telegram_chat_group_members gm "
            "JOIN public.telegram_chats c ON c.chat_id=gm.chat_id "
            "WHERE gm.group_id=%s AND gm.enabled ORDER BY gm.chat_id",
            (workspace.get('group_id'),))
        identity_chat_ids=[
            int(row['chat_id']) for row in sources
            if _norm(row.get('chat_name'))=='клиент а']

    spreadsheet_id=integration.get('spreadsheet_id')
    sheet_name=integration.get('sheet_name','Лист1')
    client=GoogleSheets(credentials_path)
    metadata=client.metadata(spreadsheet_id)
    sheets=[x.get('properties') or {} for x in metadata.get('sheets') or []]
    if len([x for x in sheets if x.get('title')==sheet_name])!=1:
        raise SheetSyncError('configured_google_sheet_tab_not_found')
    rows=client.values(spreadsheet_id,sheet_name)
    result=build_population_proposal(
        rows,canonical_rows,candidates,evidence_rows,
        context={'instance_id':instance,'workspace_id':str(workspace['id']),
                 'workspace_key':workspace['workspace_key'],
                 'spreadsheet_id':spreadsheet_id,'sheet_name':sheet_name,
                 'write_mode':integration.get('write_mode','observe'),
                 'identity_chat_ids':identity_chat_ids},
        entity_schemas=schemas,
        valid_personal_project_ids=valid_personal_project_ids)
    result['status']='observe'
    return result
