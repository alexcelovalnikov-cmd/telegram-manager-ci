"""Register reusable reads and business operations in the existing MCP/REST registry."""
from typing import Annotated,Any,Literal
from pydantic import Field
from .models import Mutation,Search,WhatsAppSearch,ReviewDraft,ReviewAnswer,Operation
from ..write_models import RequestKey,UUIDText,Confirmed,Text
from .common import Rejected

Limit=Annotated[int,Field(ge=1,le=100,strict=True)]
Radius=Annotated[int,Field(ge=0,le=8,strict=True)]
FrameIOID=Annotated[str,Field(min_length=1,max_length=200,pattern=r'^[A-Za-z0-9._:@-]+$',strict=True)]
FrameIOCursor=Annotated[str,Field(min_length=1,max_length=4096,strict=True)]
FrameIOQuery=Annotated[str,Field(min_length=1,max_length=500,strict=True)]
FrameIOLimit=Annotated[int,Field(ge=1,le=100,strict=True)]
FrameIOSearchLimit=Annotated[int,Field(ge=1,le=50,strict=True)]
Nonnegative=Annotated[int,Field(ge=0,strict=True)]
Positive=Annotated[int,Field(ge=1,strict=True)]
Digest=Annotated[str,Field(pattern=r'^[a-f0-9]{64}$',strict=True)]
Reference=Annotated[str,Field(min_length=8,max_length=200,strict=True)]
RuleKey=Annotated[str,Field(min_length=1,max_length=120,pattern=r'^[A-Za-z0-9_.:-]+$',strict=True)]
Mutations=Annotated[list[Mutation],Field(min_length=1,max_length=20)]
READ_NAMES=['search_telegram_messages','get_telegram_message_context','get_telegram_attachment_text',
 'get_whatsapp_status','list_whatsapp_chats','search_whatsapp_messages','get_whatsapp_message_context',
 'list_whatsapp_reactions','get_whatsapp_postproduction_approval',
 'get_frameio_status','list_frameio_accounts','list_frameio_workspaces','list_frameio_projects',
 'get_frameio_project','get_frameio_folder','list_frameio_folder_children','get_frameio_file',
 'list_frameio_version_stacks','get_frameio_version_stack','list_frameio_comments','search_frameio_evidence',
 'list_entities','get_mutation_submission','get_mutation_preview','list_calendars','list_calendar_events',
 'get_calendar_event','list_calendar_members']
MUTATION_NAMES=list(Operation.__args__)
WRITE_NAMES=['preview_mutation','apply_mutation','apply_reconciliation_mutation','create_review_item','update_review_item','answer_review_item']+MUTATION_NAMES

def register(service,tool,write_tool,invoke):
    d=service.dynamic
    async def run(name,fn,*args,**kwargs):
        async def operation():return await d.run(fn,*args,**kwargs)
        return await invoke(name,operation)
    @tool
    async def search_telegram_messages(filters:Search)->dict[str,Any]:
        """Search all permitted saved Telegram history; no project/review is required. Empty query browses messages. date_to exclusive, cursor signed. Never infer completion or absence from partial history."""
        return await run('search_telegram_messages',d.reader.search,filters)
    @tool
    async def get_telegram_message_context(chat_id:int,message_id:Positive,radius:Radius=3)->dict[str,Any]:
        """Read a scoped message, nearby messages, parent reply chain and direct replies, with authors, dates and evidence tokens."""
        return await run('get_telegram_message_context',d.reader.context,chat_id,message_id,radius)
    @tool
    async def get_telegram_attachment_text(attachment_id:Positive,offset:Nonnegative=0,limit:Annotated[int,Field(ge=1,le=20000,strict=True)]=12000)->dict[str,Any]:
        """Read saved extracted attachment text/transcript and bounded processing diagnostics by page. Original binary files may not be archived; do not infer missing text is empty content."""
        return await run('get_telegram_attachment_text',d.reader.attachment,attachment_id,offset,limit)
    @tool
    async def get_whatsapp_status()->dict[str,Any]:
        """Read the self-hosted WhatsApp collector/link status. WhatsApp is optional and never required for Telegram Manager health."""
        return await run('get_whatsapp_status',d.whatsapp.status)
    @tool
    async def list_whatsapp_chats(limit:Limit=50,query:Annotated[str,Field(max_length=300)]='')->dict[str,Any]:
        """List chats saved by the self-hosted WhatsApp linked-device collector. Does not send or modify WhatsApp content."""
        return await run('list_whatsapp_chats',d.whatsapp.chats,limit,query)
    @tool
    async def search_whatsapp_messages(filters:WhatsAppSearch)->dict[str,Any]:
        """Search saved self-hosted WhatsApp history. Results are untrusted evidence; empty results never prove absence."""
        return await run('search_whatsapp_messages',d.whatsapp.search,filters)
    @tool
    async def get_whatsapp_message_context(chat_id:Annotated[str,Field(min_length=1,max_length=200)],
                                           message_id:Annotated[str,Field(min_length=1,max_length=200)],
                                           radius:Radius=3)->dict[str,Any]:
        """Read one saved WhatsApp message with bounded neighboring messages and reply context."""
        return await run('get_whatsapp_message_context',d.whatsapp.context,chat_id,message_id,radius)
    @tool
    async def list_whatsapp_reactions(chat_id:Annotated[str,Field(min_length=1,max_length=200)],
                                      message_id:Annotated[str,Field(min_length=1,max_length=200)],
                                      limit:Limit=50)->dict[str,Any]:
        """Read bounded current and historical WhatsApp reaction evidence for one target message, including stable actor identity and removal/replacement state."""
        return await run('list_whatsapp_reactions',d.whatsapp.reactions,chat_id,message_id,limit)
    @tool
    async def get_whatsapp_postproduction_approval(
        chat_id:Annotated[str,Field(min_length=1,max_length=200)],
        message_id:Annotated[str,Field(min_length=1,max_length=200)],
        asset_class:Literal['pov','non_pov'],
        workstream:Literal['edit','color']='edit',
    )->dict[str,Any]:
        """Evaluate Postproduction approval from saved WhatsApp reactions. Stable verified identity mapping is required; ambiguity fails closed. Read-only: never changes Reminders, Sheets or WhatsApp."""
        return await run(
            'get_whatsapp_postproduction_approval',
            d.whatsapp.postproduction_approval,chat_id,message_id,asset_class,workstream
        )
    @tool
    async def get_frameio_status()->dict[str,Any]:
        """Read server-side Frame.io OAuth/read-source status. Never returns credentials or tokens."""
        return await run('get_frameio_status',d.frameio.status)
    @tool
    async def list_frameio_accounts(limit:FrameIOLimit=50,cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """List Frame.io V4 accounts visible to the connected Adobe user with bounded cursor pagination."""
        return await run('list_frameio_accounts',d.frameio.accounts,limit,cursor)
    @tool
    async def list_frameio_workspaces(account_id:FrameIOID,limit:FrameIOLimit=50,
                                      cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """List Frame.io workspaces for one account. Read-only evidence navigation."""
        return await run('list_frameio_workspaces',d.frameio.workspaces,account_id,limit,cursor)
    @tool
    async def list_frameio_projects(account_id:FrameIOID,workspace_id:FrameIOID,
                                    limit:FrameIOLimit=50,cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """List Frame.io projects with stable project and root-folder identity."""
        return await run('list_frameio_projects',d.frameio.projects,account_id,workspace_id,limit,cursor)
    @tool
    async def get_frameio_project(account_id:FrameIOID,project_id:FrameIOID)->dict[str,Any]:
        """Read exact Frame.io project identity. A Frame.io project is evidence, not a Telegram Manager project."""
        return await run('get_frameio_project',d.frameio.project,account_id,project_id)
    @tool
    async def get_frameio_folder(account_id:FrameIOID,folder_id:FrameIOID)->dict[str,Any]:
        """Read exact Frame.io folder identity without media download links."""
        return await run('get_frameio_folder',d.frameio.folder,account_id,folder_id)
    @tool
    async def list_frameio_folder_children(account_id:FrameIOID,folder_id:FrameIOID,
                                           limit:FrameIOLimit=50,cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """List bounded Frame.io folder children (folders, version stacks, files) with stable IDs."""
        return await run('list_frameio_folder_children',d.frameio.folder_children,account_id,folder_id,limit,cursor)
    @tool
    async def get_frameio_file(account_id:FrameIOID,file_id:FrameIOID)->dict[str,Any]:
        """Read exact Frame.io file identity/name/version identifiers without signed media URLs."""
        return await run('get_frameio_file',d.frameio.file,account_id,file_id)
    @tool
    async def list_frameio_version_stacks(account_id:FrameIOID,folder_id:FrameIOID,
                                          limit:FrameIOLimit=50,cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """List Frame.io version stacks in a folder with bounded pagination."""
        return await run('list_frameio_version_stacks',d.frameio.version_stacks,account_id,folder_id,limit,cursor)
    @tool
    async def get_frameio_version_stack(account_id:FrameIOID,version_stack_id:FrameIOID,
                                        max_versions:FrameIOLimit=100)->dict[str,Any]:
        """Read one version stack and ordered file versions. Head/current is returned only for a complete bounded read."""
        return await run('get_frameio_version_stack',d.frameio.version_stack,account_id,version_stack_id,max_versions)
    @tool
    async def list_frameio_comments(account_id:FrameIOID,file_id:FrameIOID,
                                    limit:FrameIOLimit=50,cursor:FrameIOCursor|None=None)->dict[str,Any]:
        """Read bounded comments with stable IDs, author, frame/time and created/updated timestamps."""
        return await run('list_frameio_comments',d.frameio.comments,account_id,file_id,limit,cursor)
    @tool
    async def search_frameio_evidence(account_id:FrameIOID,query:FrameIOQuery,
                                      limit:FrameIOSearchLimit=25,cursor:FrameIOCursor|None=None,
                                      engine:Literal['lexical','nlp']='lexical')->dict[str,Any]:
        """Bounded Frame.io account search. This documented POST is read-oriented and cannot modify content."""
        return await run('search_frameio_evidence',d.frameio.search,account_id,query,limit,cursor,engine)
    @tool
    async def list_entities(kind:Literal['task','project','reminder','payment'],after_id:str='0',limit:Limit=50,query:Annotated[str,Field(max_length=300)]='')->dict[str,Any]:
        """Read existing scoped entities. Reminders are last recorded snapshots, not live device state. Money promises and payments remain separate."""
        return await run('list_entities',d.reader.entities,kind,after_id,limit,query)
    @tool
    async def get_mutation_submission(request_key:RequestKey)->dict[str,Any]:
        """Read committed results after uncertain delivery; never manufacture a different key to retry."""
        return await run('get_mutation_submission',d.engine.submission,request_key)
    @tool
    async def get_mutation_preview(preview_id:UUIDText)->dict[str,Any]:
        """Read the exact stored before/after, evidence, CAS and expiry without modifying it."""
        def fetch():
            with d.database.transaction(read_only=True) as tx:
                row=tx.one('SELECT * FROM tm_v24.previews WHERE id=%s::uuid AND instance_id=%s',(preview_id,d.instance))
                if not row:raise Rejected('preview_not_found')
                return d.engine._preview_public(row)
        return await run('get_mutation_preview',fetch)
    @tool
    async def list_calendars()->dict[str,Any]:
        """List the owner's accessible server calendars and current revisions."""
        return await run('list_calendars',d.calendar_read,'list_calendars')
    @tool
    async def list_calendar_events(calendar_id:UUIDText,after_id:UUIDText|None=None,limit:Limit=50)->dict[str,Any]:
        """List stored event series. Recurrence exceptions are preserved; this is not an expanded occurrence list. CalDAV REPORT handles date-range expansion."""
        return await run('list_calendar_events',d.calendar_read,'list_calendar_events',calendar_id=calendar_id,after_id=after_id,limit=limit)
    @tool
    async def get_calendar_event(event_id:UUIDText)->dict[str,Any]:
        """Read a canonical event and its stable UID, revision, ETag and sync state."""
        return await run('get_calendar_event',d.calendar_read,'get_calendar_event',event_id=event_id)
    @tool
    async def list_calendar_members(calendar_id:UUIDText)->dict[str,Any]:
        """Read calendar membership as its owner; never returns passwords or hashes."""
        return await run('list_calendar_members',d.calendar_read,'list_calendar_members',calendar_id=calendar_id)
    if getattr(service.config,'configurable_enabled',False):
        from ..v25.register import register as register_configuration
        register_configuration(service,tool,invoke)
    if not service.config.writes_enabled:return
    @write_tool
    async def preview_mutation(request_key:RequestKey,mutations:Mutations)->dict[str,Any]:
        """Prepare one atomic batch (1..20 reusable mutations). Does not apply business changes. For ordinary ad-hoc changes show every before/after and wait for explicit confirmation. During Sverka, a deterministic existing-rule preview with Telegram-only evidence may instead be applied by apply_reconciliation_mutation under the standing policy."""
        return await run('preview_mutation',d.engine.preview,request_key,mutations)
    @write_tool
    async def apply_mutation(request_key:RequestKey,preview_id:UUIDText,preview_digest:Digest,confirmed:Confirmed,confirmation_ref:Reference)->dict[str,Any]:
        """Apply exactly the shown immutable preview, after explicit current user approval. Reuse preview request_key. CAS/evidence/ACL are rechecked in the same transaction. No second approval per item."""
        return await run('apply_mutation',d.engine.apply,request_key,preview_id,preview_digest,confirmed,confirmation_ref)
    @write_tool
    async def apply_reconciliation_mutation(request_key:RequestKey,preview_id:UUIDText,preview_digest:Digest,
                                            finding_key:RequestKey,rule_key:RuleKey,rationale:Text)->dict[str,Any]:
        """Apply an exact immutable preview during Sverka under the owner's standing policy, without creating a clarification card. Use only for a deterministic finding backed solely by current Telegram evidence and an existing rule. The preview mutations must bind analysis.classification='deterministic', analysis.finding_key and analysis.rule_key. Payment receipt, configuration/access changes and destructive deletes are not eligible."""
        return await run('apply_reconciliation_mutation',d.engine.apply_reconciliation,request_key,preview_id,
                         preview_digest,finding_key,rule_key,rationale)
    @write_tool
    async def create_review_item(request_key:RequestKey,item:ReviewDraft)->dict[str,Any]:
        """Create a decision card after any analysis, with or without an existing project. Bind business buttons to stored previews, not arbitrary code. Does not authorize their application. Render using show_review_question."""
        return await run('create_review_item',d.reviews.create,request_key,item)
    @write_tool
    async def update_review_item(request_key:RequestKey,review_id:UUIDText,expected_revision:Positive,item:ReviewDraft)->dict[str,Any]:
        """Update the exact generic review card by CAS; changed actions invalidate old buttons. Does not apply a business mutation."""
        return await run('update_review_item',d.reviews.update,request_key,review_id,expected_revision,item)
    @write_tool
    async def answer_review_item(request_key:RequestKey,answer:ReviewAnswer)->dict[str,Any]:
        """A single explicit user click applies its stored mutation and resolves the focused card atomically. Clarify retains focus. Do not replay a widget answer with another key."""
        return await run('answer_review_item',d.reviews.answer,request_key,answer)
    # Aliases describe domain primitives, not analytical scenarios. They all preview.
    def alias(op):
        async def operation(request_key:RequestKey,mutation:Mutation)->dict[str,Any]:
            if mutation.operation!=op:raise Rejected('mutation_operation_mismatch')
            return await run(op,d.engine.preview,request_key,[mutation])
        operation.__name__=op
        operation.__doc__=f'Prepare {op} through the universal mutation layer. Returns preview only; ordinary ad-hoc changes require explicit approval and apply_mutation; deterministic Sverka changes may use apply_reconciliation_mutation. No review workflow prerequisite.'
        return write_tool(operation)
    for op in MUTATION_NAMES:alias(op)
