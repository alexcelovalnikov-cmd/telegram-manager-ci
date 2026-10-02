"""Validated DATA, never server-side Python/SQL or unrestricted template execution.

Natural-language interpretation is performed by the calling assistant using the
versioned analysis context. This module does not pretend to be a semantic model.
"""
import re
from copy import deepcopy
from string import Formatter
from typing import Literal
from pydantic import Field, model_validator
from tm_api.write_models import Strict
from tm_api.v24.common import Rejected, encoded

KEY = r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$'
UUID_RE = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
ENTITY_META_FIELDS = {'parent_id'}

class Rule(Strict):
    text: str = Field(min_length=3, max_length=8000)
    applies_to: list[str] = Field(default_factory=list, max_length=40)
    decision_key: str | None = Field(default=None, pattern=KEY)
    value: object = None
    priority: int = Field(default=100, ge=0, le=1000)
    examples: list[str] = Field(default_factory=list, max_length=15)
    @model_validator(mode='after')
    def bounded(self):
        if len(encoded(self.model_dump()).encode()) > 16000:
            raise ValueError('Rule is too large')
        if any(len(v)>1000 for v in self.examples): raise ValueError('Example too large')
        return self

class Template(Strict):
    entity_kind: str = Field(pattern=KEY)
    channel: Literal['title','description']
    pattern: str = Field(max_length=6000)
    @model_validator(mode='after')
    def safe(self):
        validate_template(self.pattern)
        return self

class EntityType(Strict):
    label: str = Field(min_length=1, max_length=150)
    fields: dict = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list, max_length=50)
    states: list[str] = Field(default_factory=list, max_length=50)
    reminder_projection: bool = False
    @model_validator(mode='after')
    def schema(self):
        if len(self.fields)>50: raise ValueError('Too many fields')
        for key,spec in self.fields.items():
            if key in ENTITY_META_FIELDS: raise ValueError('Reserved entity metadata field')
            if not re.fullmatch(KEY,key) or not isinstance(spec,dict): raise ValueError('Invalid field')
            if set(spec)-{'type','label','enum','max_length'}: raise ValueError('Unsupported field option')
            if spec.get('type') not in ('text','date','datetime','decimal','integer','boolean','text_list'):
                raise ValueError('Unsupported field type')
            if 'max_length' in spec and (type(spec['max_length']) is not int or not 1<=spec['max_length']<=16000):
                raise ValueError('Invalid max_length')
            if 'enum' in spec and (not isinstance(spec['enum'],list) or len(spec['enum'])>100):
                raise ValueError('Invalid enum')
        if not set(self.required)<=set(self.fields): raise ValueError('Unknown required field')
        if any(not isinstance(s,str) or not re.fullmatch(KEY,s) for s in self.states): raise ValueError('Invalid states')
        return self

class Document(Strict):
    key: str = Field(pattern=KEY)
    kind: Literal['rule','template','entity_type']
    body: dict
    status: Literal['proposed','active','disabled','deleted'] = 'active'
    origin: Literal['explicit_user','review_generalization','import'] = 'explicit_user'
    review_id: str | None = Field(default=None, max_length=80)
    @model_validator(mode='after')
    def typed(self):
        cls={'rule':Rule,'template':Template,'entity_type':EntityType}[self.kind]
        self.body=cls.model_validate(self.body).model_dump()
        if self.origin=='review_generalization' and self.kind!='rule':
            raise ValueError('Learning produces only rule proposals')
        return self

class Destination(Strict):
    mode: Literal['none','server_new','server_existing','legacy_existing'] = 'none'
    id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    @model_validator(mode='after')
    def shape(self):
        if self.mode=='server_new' and (not self.title or self.id): raise ValueError('New list needs title only')
        if self.mode in ('server_existing','legacy_existing') and not self.id: raise ValueError('Existing list ID required')
        if self.mode=='none' and (self.id or self.title): raise ValueError('No destination takes no ID/title')
        return self

class SourceSelector(Strict):
    kind: Literal['username','title']
    value: str = Field(min_length=1,max_length=300)
    @model_validator(mode='after')
    def normalized(self):
        self.value=self.value.strip()
        if self.kind=='username':
            self.value=self.value.lstrip('@')
            if not re.fullmatch(r'[A-Za-z0-9_]{3,64}',self.value):raise ValueError('Invalid Telegram username')
        if not self.value or '\x00' in self.value:raise ValueError('Empty source selector')
        return self

class Workspace(Strict):
    key: str = Field(pattern=KEY)
    name: str = Field(min_length=1, max_length=200)
    existing_group_key: str | None = Field(default=None, max_length=120)
    chat_ids: list[int] = Field(default_factory=list, max_length=200)
    reminder_list: Destination = Field(default_factory=Destination)
    source_selectors: list[SourceSelector] = Field(default_factory=list,max_length=200)
    default_tags: list[str] = Field(default_factory=list,max_length=30)
    calendar_id: str | None = None
    timezone: str = 'Asia/Yekaterinburg'
    analysis: dict = Field(default_factory=dict)
    formatting: dict = Field(default_factory=dict)
    integrations: dict = Field(default_factory=dict)
    documents: list[Document] = Field(default_factory=list, max_length=60)
    @model_validator(mode='after')
    def bounded(self):
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)
        if len(set(self.chat_ids))!=len(self.chat_ids) or any(type(i) is not int or i==0 for i in self.chat_ids):
            raise ValueError('Use unique resolved Telegram chat IDs')
        if len({d.key for d in self.documents})!=len(self.documents): raise ValueError('Duplicate document key')
        if any(not t or len(t)>100 or '\x00' in t for t in self.default_tags):raise ValueError('Invalid default tags')
        if len({(v.kind,v.value) for v in self.source_selectors})!=len(self.source_selectors):raise ValueError('Duplicate source selectors')
        validate_analysis(self.analysis);validate_formatting(self.formatting);validate_integrations(self.integrations)
        return self

def validate_analysis(data):
    if not isinstance(data,dict) or set(data)-{'instructions','tracked_kinds','review_policy','default_entity_type','profile'}:
        raise Rejected('invalid_analysis_configuration')
    if len(encoded(data).encode())>18000:raise Rejected('analysis_configuration_too_large')
    if 'instructions' in data and (not isinstance(data['instructions'],str) or len(data['instructions'])>12000):raise Rejected('invalid_instructions')
    if 'tracked_kinds' in data and (not isinstance(data['tracked_kinds'],list) or len(data['tracked_kinds'])>60 or any(not isinstance(x,str) or len(x)>100 for x in data['tracked_kinds'])):raise Rejected('invalid_tracked_kinds')
    if data.get('review_policy','approval_required') not in ('approval_required','ambiguous_only'):
        raise Rejected('invalid_review_policy')
    if data.get('profile') is not None and data.get('profile') not in ('postproduction','rcc_settlement'):
        raise Rejected('invalid_analysis_profile')
    # review_policy changes question presentation, NEVER authorization to write.
    return data

def validate_integrations(data):
    if not isinstance(data,dict) or set(data)-{'google_sheets'}:
        raise Rejected('invalid_integrations_configuration')
    value=data.get('google_sheets')
    if value is None:return data
    if not isinstance(value,dict) or set(value)-{'enabled','mode','spreadsheet_id','sheet_name','write_mode'}:
        raise Rejected('invalid_google_sheets_integration')
    if type(value.get('enabled',True)) is not bool:
        raise Rejected('invalid_google_sheets_enabled')
    if value.get('mode')!='rcc_settlement':
        raise Rejected('unsupported_google_sheets_mode')
    spreadsheet_id=value.get('spreadsheet_id')
    if not isinstance(spreadsheet_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}',spreadsheet_id):
        raise Rejected('invalid_google_spreadsheet_id')
    sheet_name=value.get('sheet_name','Лист1')
    if not isinstance(sheet_name,str) or not 1<=len(sheet_name)<=200 or '\x00' in sheet_name:
        raise Rejected('invalid_google_sheet_name')
    if value.get('write_mode','observe') not in ('observe','managed'):
        raise Rejected('invalid_google_sheets_write_mode')
    return data

def validate_formatting(data):
    allowed={'received_marker','awaiting_marker','partial_separator','thousands_suffix','decimal_separator','currency_suffix','tag_separator','date_format'}
    if not isinstance(data,dict) or set(data)-allowed:raise Rejected('invalid_formatting_configuration')
    if any(not isinstance(v,str) or len(v)>80 or '\x00' in v for v in data.values()):raise Rejected('invalid_format_token')
    if 'date_format' in data and data['date_format'] not in ('MM.DD','DD.MM','YYYY-MM-DD'):
        raise Rejected('invalid_date_format')
    return data

def validate_template(pattern):
    try:
        for literal,field,spec,conversion in Formatter().parse(pattern):
            if field is None:continue
            if spec or conversion or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*',field) or '__' in field:
                raise Rejected('unsafe_template_placeholder')
    except ValueError:raise Rejected('invalid_template') from None
    return pattern

def render(pattern,values,max_length=16000):
    validate_template(pattern);out=[]
    for literal,field,spec,conversion in Formatter().parse(pattern):
        out.append(literal)
        if field is None:continue
        value=values
        for part in field.split('.'):
            if not isinstance(value,dict) or part not in value:raise Rejected('template_field_missing:'+field)
            value=value[part]
        if value is None:value=''
        if not isinstance(value,(str,int,bool)):raise Rejected('template_field_not_scalar:'+field)
        out.append(str(value))
    text=''.join(out).strip()
    if len(text)>max_length or '\x00' in text:raise Rejected('rendered_text_too_large')
    return text

def validate_entity(definition,data):
    from datetime import date,datetime
    from .finance import money
    spec=EntityType.model_validate(definition)
    if not isinstance(data,dict) or set(data)-set(spec.fields)-{'title','description','state'}-ENTITY_META_FIELDS:
        raise Rejected('unknown_entity_field')
    if set(spec.required)-set(data):raise Rejected('required_entity_field_missing')
    if data.get('state') is not None and data['state'] not in spec.states:raise Rejected('invalid_entity_state')
    for key,value in data.items():
        if key=='parent_id':
            if value is not None and (not isinstance(value,str) or not re.fullmatch(UUID_RE,value)):
                raise Rejected('invalid_entity_parent_id')
            continue
        if key in ('title','description','state'):
            if not isinstance(value,str) or len(value)>(500 if key=='title' else 16000):raise Rejected('invalid_entity_text')
            continue
        f=spec.fields[key];t=f['type']
        if t=='text' and (not isinstance(value,str) or len(value)>f.get('max_length',16000)):raise Rejected('invalid_entity_text')
        if t=='integer' and (type(value) is not int or abs(value)>10**12):raise Rejected('invalid_entity_integer')
        if t=='boolean' and type(value) is not bool:raise Rejected('invalid_entity_boolean')
        if t=='decimal':money(value)
        if t=='date':date.fromisoformat(value)
        if t=='datetime':
            if datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is None:raise Rejected('entity_timezone_required')
        if t=='text_list' and (not isinstance(value,list) or len(value)>100 or any(not isinstance(x,str) or len(x)>1000 for x in value)):raise Rejected('invalid_entity_list')
        if 'enum' in f and value not in f['enum']:raise Rejected('invalid_entity_enum')
    if len(encoded(data).encode())>24000:raise Rejected('entity_too_large')
    return deepcopy(data)
