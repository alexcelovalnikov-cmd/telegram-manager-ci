"""Server-side Google Sheets projection for RCC client settlements.

No credential content is returned or logged. Only the exact configured spreadsheet
ID is contacted. Unbound legacy rows are preserved and contribute to the unpaid
total; only rows marked tm-v75 are rebuilt from canonical server entities.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from tm_api.v24.common import Rejected
from .settlement import read_summary

MARKER='tm-v75'
CUTOVER_CONTRACT='tm-rcc-settlement-cutover/v1'
MAX_RESPONSE=2_000_000
METADATA_HEADER=['TM Project ID','TM Item ID','TM Revision','TM Managed']
PROJECT_BG={'red':0.35686275,'green':0.24705882,'blue':0.5254902}
ITEM_BG={'red':0.8509804,'green':0.8235294,'blue':0.9137255}
WHITE={'red':1,'green':1,'blue':1}
DARK_TEXT={'red':0.2627451,'green':0.2627451,'blue':0.2627451}

class SheetSyncError(RuntimeError):
    pass

def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()

class GoogleSheets:
    def __init__(self,credentials_path):
        path=Path(credentials_path or '')
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size>20000:
            raise SheetSyncError('google_credentials_unavailable')
        try:
            value=json.loads(path.read_text())
        except Exception:
            raise SheetSyncError('google_credentials_invalid') from None
        if (value.get('type')!='service_account'
                or value.get('token_uri')!='https://oauth2.googleapis.com/token'
                or not str(value.get('client_email') or '').endswith('.gserviceaccount.com')
                or not str(value.get('private_key') or '').startswith('-----BEGIN PRIVATE KEY-----')):
            raise SheetSyncError('google_credentials_invalid')
        self.value=value
        self._token=None
        self._expires=0

    def token(self):
        now=int(time.time())
        if self._token and now<self._expires-60:
            return self._token
        header=_b64(json.dumps({'alg':'RS256','typ':'JWT'},separators=(',',':')).encode())
        claim=_b64(json.dumps({'iss':self.value['client_email'],
            'scope':'https://www.googleapis.com/auth/spreadsheets',
            'aud':'https://oauth2.googleapis.com/token','iat':now,'exp':now+3600},
            separators=(',',':')).encode())
        unsigned=(header+'.'+claim).encode()
        try:
            key=serialization.load_pem_private_key(self.value['private_key'].encode(),password=None)
            signature=key.sign(unsigned,padding.PKCS1v15(),hashes.SHA256())
        except Exception:
            raise SheetSyncError('google_credentials_invalid') from None
        assertion=unsigned.decode()+'.'+_b64(signature)
        body=urllib.parse.urlencode({'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion}).encode()
        req=urllib.request.Request('https://oauth2.googleapis.com/token',data=body,
            headers={'Content-Type':'application/x-www-form-urlencoded'})
        payload=self._open(req,'token')
        token=payload.get('access_token')
        if not isinstance(token,str) or not token:
            raise SheetSyncError('google_token_missing')
        self._token=token
        self._expires=now+int(payload.get('expires_in') or 3600)
        return token

    def _open(self,request,kind='sheets'):
        try:
            with urllib.request.urlopen(request,timeout=15) as response:
                raw=response.read(MAX_RESPONSE+1)
        except urllib.error.HTTPError as exc:
            code=exc.code
            if kind=='token':
                raise SheetSyncError('google_credentials_rejected') from None
            if code==403:raise SheetSyncError('google_sheets_forbidden_or_api_disabled') from None
            if code==404:raise SheetSyncError('google_spreadsheet_or_sheet_not_found') from None
            if code==401:raise SheetSyncError('google_credentials_rejected') from None
            raise SheetSyncError('google_sheets_http_'+str(code)) from None
        except Exception:
            raise SheetSyncError('google_network_unavailable') from None
        if len(raw)>MAX_RESPONSE:
            raise SheetSyncError('google_response_too_large')
        try:return json.loads(raw or b'{}')
        except Exception:raise SheetSyncError('google_invalid_response') from None

    def call(self,method,spreadsheet_id,suffix='',payload=None,query=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,200}',spreadsheet_id):
            raise SheetSyncError('invalid_google_spreadsheet_id')
        url='https://sheets.googleapis.com/v4/spreadsheets/'+spreadsheet_id+suffix
        if query:url+='?'+urllib.parse.urlencode(query)
        data=None if payload is None else json.dumps(payload,separators=(',',':')).encode()
        req=urllib.request.Request(url,data=data,method=method,headers={
            'Authorization':'Bearer '+self.token(),'Content-Type':'application/json'})
        return self._open(req)

    def metadata(self,spreadsheet_id):
        return self.call('GET',spreadsheet_id,query={'fields':'spreadsheetId,properties.title,sheets.properties(sheetId,title)'})

    def values(self,spreadsheet_id,sheet_name):
        rng="'"+sheet_name.replace("'","''")+"'!A1:J1000"
        suffix='/values/'+urllib.parse.quote(rng,safe='')
        return self.call('GET',spreadsheet_id,suffix,query={'valueRenderOption':'UNFORMATTED_VALUE'}).get('values') or []

    def structural(self,spreadsheet_id,requests):
        if requests:self.call('POST',spreadsheet_id,':batchUpdate',{'requests':requests})

    def write_values(self,spreadsheet_id,data):
        if data:self.call('POST',spreadsheet_id,'/values:batchUpdate',{'valueInputOption':'RAW','data':data})

def _number(value):
    if value in (None,''):return None
    if isinstance(value,bool):return None
    if isinstance(value,(int,float)):return Decimal(str(value))
    if isinstance(value,str):
        text=value.replace('\u00a0',' ').replace('\u202f',' ').replace('₽','').replace('руб.','').replace('руб','')
        text=re.sub(r'\s+','',text).replace(',','.')
        try:return Decimal(text)
        except InvalidOperation:return None
    return None

def _paid(value):
    if value is True:return True
    if isinstance(value,str) and value.strip().casefold() in ('true','истина','да','yes'):return True
    return False

def _groups(indices):
    if not indices:return []
    values=sorted(set(indices));out=[];start=prev=values[0]
    for value in values[1:]:
        if value==prev+1:prev=value;continue
        out.append((start,prev+1));start=prev=value
    out.append((start,prev+1))
    return out

def _load(database,instance):
    with database.transaction(read_only=True) as tx:
        rows=tx.all("SELECT id,workspace_key,name,settings FROM tm_config.workspaces WHERE instance_id=%s AND status='active' ORDER BY id",(instance,))
        candidates=[]
        for row in rows:
            integration=((row.get('settings') or {}).get('integrations') or {}).get('google_sheets')
            if isinstance(integration,dict) and integration.get('mode')=='rcc_settlement' and integration.get('enabled',True):
                candidates.append((row,integration))
        if not candidates:
            return None
        if len(candidates)!=1:
            raise SheetSyncError('multiple_rcc_sheet_integrations')
        workspace,integration=candidates[0]
        summary=read_summary(tx,instance,str(workspace['id']))
        return {'workspace':workspace,'integration':integration,'summary':summary}

def _row10(row):
    return (list(row)+['']*10)[:10]

def _norm(value):
    return ' '.join(str(value or '').strip().casefold().split())

def _excluded_project_title(value):
    normalized=_norm(value)
    return normalized=='готово боев' or normalized.startswith('монтаж 6 роликов pov')

def _total_layout(values):
    for i,row in enumerate(values):
        if _norm(row[0])=='итого:':
            return i,0,1
        if _norm(row[1])=='итого:':
            return i,1,2
    return len(values),1,2

def _column_name(index):
    if type(index) is not int or index<0:
        raise SheetSyncError('invalid_sheet_column_index')
    value=index+1;out=''
    while value:
        value,rem=divmod(value-1,26)
        out=chr(65+rem)+out
    return out

def _digest(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()
    return hashlib.sha256(raw).hexdigest()

def _canonical_index(summary):
    projects=[]
    project_ids=set()
    item_ids=set()
    excluded=[]
    for project in summary.get('projects') or []:
        project_id=str(project.get('id') or '')
        if not project_id or project_id in project_ids:
            raise SheetSyncError('duplicate_or_missing_canonical_project_id')
        project_ids.add(project_id)
        items=[]
        for item in project.get('items') or []:
            if not item.get('client_settlement'):
                continue
            item_id=str(item.get('id') or '')
            if not item_id or item_id in item_ids:
                raise SheetSyncError('duplicate_or_missing_canonical_item_id')
            if str(item.get('project_id') or '')!=project_id:
                raise SheetSyncError('canonical_item_parent_mismatch')
            item_ids.add(item_id)
            items.append(item)
        if not items:
            continue
        if _excluded_project_title(project.get('title')):
            excluded.append({'project_id':project_id,'title':project.get('title') or '',
                'item_ids':[str(item['id']) for item in items]})
            continue
        projects.append((project,items))
    projected_project_ids={str(project['id']) for project,_ in projects}
    projected_item_ids={str(item['id']) for _,items in projects for item in items}
    excluded_project_ids={x['project_id'] for x in excluded}
    excluded_item_ids={item_id for x in excluded for item_id in x['item_ids']}
    return {'projects':projects,'project_ids':projected_project_ids,'item_ids':projected_item_ids,
            'excluded':excluded,'excluded_project_ids':excluded_project_ids,'excluded_item_ids':excluded_item_ids}

def _canonical_block(index,project_titles=None,item_titles=None):
    project_titles=project_titles or {}
    item_titles=item_titles or {}
    a_to_e=[];g_to_j=[];entities=[]
    canonical_remaining=Decimal(0)
    for project,items in index['projects']:
        project_id=str(project['id'])
        title=project_titles.get(project_id) or project['title']
        a_to_e.append([title,'','',False,''])
        g_to_j.append([project_id,'',project['revision'],MARKER])
        entities.append({'kind':'project','project_id':project_id,'item_id':None})
        for item in items:
            item_id=str(item['id'])
            title=item_titles.get(item_id) or item['title']
            a_to_e.append(['',title,float(Decimal(item['amount_rub'])),bool(item['paid']),item['payment_display']])
            g_to_j.append([project_id,item_id,item['revision'],MARKER])
            entities.append({'kind':'item','project_id':project_id,'item_id':item_id})
            canonical_remaining+=Decimal(item['remaining_rub'])
    return a_to_e,g_to_j,entities,canonical_remaining

def _legacy_duplicate_blockers(values,total_index,managed,index,bound_project_rows):
    blockers=[]
    managed_set=set(managed)
    legacy_projects={}
    current_project_title=''
    current_project_id=None
    for i,row in enumerate(values):
        if i==0 or i>=total_index:
            continue
        a=_norm(row[0]);b=_norm(row[1])
        if a and not b:
            current_project_title=str(row[0]).strip()
            current_project_id=str(row[6] or '') if i in managed_set else None
            if i not in managed_set:
                legacy_projects.setdefault(_norm(current_project_title),[]).append(i)
            continue
        if i in managed_set or not b:
            continue
        if current_project_id and current_project_id in bound_project_rows:
            amount=_number(row[2])
            for project,items in index['projects']:
                if str(project['id'])!=current_project_id:
                    continue
                for item in items:
                    if str(item['id']) in {str(values[j][7] or '') for j in managed_set}:
                        continue
                    if _norm(item['title'])==b and amount is not None and amount==Decimal(item['amount_rub']):
                        blockers.append({'code':'unbound_legacy_item_matches_canonical',
                            'sheet_row':i+1,'project_id':current_project_id,'item_id':str(item['id'])})
                break
    for project,_items in index['projects']:
        project_id=str(project['id'])
        if project_id in bound_project_rows:
            continue
        rows=legacy_projects.get(_norm(project['title'])) or []
        for i in rows:
            blockers.append({'code':'unbound_legacy_project_matches_canonical',
                'sheet_row':i+1,'project_id':project_id})
    return blockers

def plan_rows(rows,summary):
    values=[_row10(row) for row in rows]
    index=_canonical_index(summary)
    project_ids=index['project_ids'];item_ids=index['item_ids']
    total_index,total_label_column,total_amount_column=_total_layout(values)
    managed=[]
    legacy_remaining=Decimal(0)
    legacy_unpaid_excluded=Decimal(0)
    legacy_unpaid_requires_population=Decimal(0)
    legacy_unpaid_excluded_rows=[]
    legacy_unpaid_requires_population_rows=[]
    blockers=[]
    project_titles={}
    item_titles={}
    bound_project_rows={}
    bound_item_rows={}
    preserved_unbound=[]
    current_project_title=''
    for i,row in enumerate(values):
        if i>0 and i<total_index and str(row[0] or '').strip() and not str(row[1] or '').strip():
            current_project_title=str(row[0]).strip()
        marker=str(row[9] or '')
        project_id=str(row[6] or '')
        item_id=str(row[7] or '')
        excluded_bound=project_id in index['excluded_project_ids'] or item_id in index['excluded_item_ids']
        is_managed=not excluded_bound and (
            marker==MARKER or item_id in item_ids or (project_id in project_ids and not str(row[1] or '').strip()))
        if is_managed:
            managed.append(i)
            if row[5] not in ('',None):
                blockers.append({'code':'managed_row_uses_protected_ready_column','sheet_row':i+1})
            if item_id in item_ids:
                if item_id in bound_item_rows:
                    blockers.append({'code':'duplicate_bound_canonical_item','sheet_row':i+1,'item_id':item_id,
                        'other_sheet_row':bound_item_rows[item_id]+1})
                else:
                    bound_item_rows[item_id]=i
                    if str(row[1] or '').strip():
                        item_titles[item_id]=str(row[1]).strip()
            elif project_id in project_ids and not str(row[1] or '').strip():
                if project_id in bound_project_rows:
                    blockers.append({'code':'duplicate_bound_canonical_project','sheet_row':i+1,'project_id':project_id,
                        'other_sheet_row':bound_project_rows[project_id]+1})
                else:
                    bound_project_rows[project_id]=i
                    if str(row[0] or '').strip():
                        project_titles[project_id]=str(row[0]).strip()
            elif marker==MARKER:
                blockers.append({'code':'managed_marker_unknown_canonical_id','sheet_row':i+1,
                    'project_id':project_id or None,'item_id':item_id or None})
            continue
        if i==0 or i>=total_index:
            continue
        if any(value not in ('',None) for value in row[:6]):
            preserved_unbound.append({'sheet_row':i+1,'values':row[:6]})
        if str(row[1] or '').strip():
            amount=_number(row[2])
            if amount is not None and amount>0 and not _paid(row[3]):
                legacy_remaining+=amount
                entry={'sheet_row':i+1,'project_title':current_project_title,
                    'item_title':str(row[1]).strip(),'amount_rub':str(amount)}
                if _excluded_project_title(current_project_title):
                    legacy_unpaid_excluded+=amount
                    legacy_unpaid_excluded_rows.append(entry)
                else:
                    legacy_unpaid_requires_population+=amount
                    legacy_unpaid_requires_population_rows.append(entry)

    blockers.extend(_legacy_duplicate_blockers(values,total_index,managed,index,bound_project_rows))
    a_to_e,g_to_j,entities,canonical_remaining=_canonical_block(index,project_titles,item_titles)
    total=legacy_remaining+canonical_remaining
    before_total=sum(1 for i in managed if i<total_index)
    total_after_delete=total_index-before_total
    canonical_rows=[a+['']+g for a,g in zip(a_to_e,g_to_j)]

    managed_set=set(managed)
    remaining=[row[:] for i,row in enumerate(values) if i not in managed_set]
    insert_at=total_after_delete
    projected=remaining[:insert_at]+[row[:] for row in canonical_rows]+remaining[insert_at:]
    if projected:
        projected[0][6:10]=METADATA_HEADER
    if total_index<len(values):
        projected_total_index=insert_at+len(canonical_rows)
        if projected_total_index>=len(projected):
            raise SheetSyncError('total_row_projection_mismatch')
        projected[projected_total_index][total_amount_column]=float(total)
    else:
        projected_total_index=insert_at+len(canonical_rows)
        total_row=['']*10
        total_row[total_label_column]='Итого:'
        total_row[total_amount_column]=float(total)
        projected.append(total_row)

    before_by_identity={}
    stale_managed=[]
    for i in managed:
        row=values[i]
        project_id=str(row[6] or '')
        item_id=str(row[7] or '')
        key=('item',item_id) if item_id in item_ids else ('project',project_id) if project_id in project_ids else None
        if key and key not in before_by_identity:
            before_by_identity[key]=(i,row)
        elif key is None:
            stale_managed.append((i,row))
    operations=[]
    for offset,(entity,row) in enumerate(zip(entities,canonical_rows)):
        key=(entity['kind'],entity['item_id'] if entity['kind']=='item' else entity['project_id'])
        before=before_by_identity.get(key)
        operations.append({'action':'rebuild_bound' if before else 'insert_canonical',
            'kind':entity['kind'],'project_id':entity['project_id'],'item_id':entity['item_id'],
            'before':None if before is None else {'sheet_row':before[0]+1,'values':before[1]},
            'after':{'sheet_row':insert_at+offset+1,'values':row}})
    for i,row in stale_managed:
        operations.append({'action':'remove_stale_managed','kind':'unknown','project_id':str(row[6] or '') or None,
            'item_id':str(row[7] or '') or None,'before':{'sheet_row':i+1,'values':row},'after':None})
    before_total_row=values[total_index] if total_index<len(values) else None
    operations.append({'action':'update_total','kind':'total',
        'before':None if before_total_row is None else {'sheet_row':total_index+1,'values':before_total_row},
        'after':{'sheet_row':projected_total_index+1,'values':projected[projected_total_index]}})
    before_header=(values[0][6:10] if values else ['','','',''])
    if before_header!=METADATA_HEADER:
        operations.append({'action':'set_metadata_header','kind':'metadata',
            'before':{'sheet_row':1,'values':before_header},'after':{'sheet_row':1,'values':METADATA_HEADER}})

    source_fingerprint=_digest(values)
    canonical_fingerprint=_digest(summary)
    return {'managed_rows':managed,'delete_groups':_groups(managed),'insert_at':insert_at,
            'a_to_e':a_to_e,'g_to_j':g_to_j,'canonical_rows':canonical_rows,'entities':entities,
            'total_label_column':total_label_column,'total_amount_column':total_amount_column,
            'legacy_remaining':legacy_remaining,
            'legacy_unpaid_excluded':legacy_unpaid_excluded,
            'legacy_unpaid_requires_population':legacy_unpaid_requires_population,
            'legacy_unpaid_excluded_rows':legacy_unpaid_excluded_rows,
            'legacy_unpaid_requires_population_rows':legacy_unpaid_requires_population_rows,
            'canonical_remaining':canonical_remaining,'total':total,
            'had_total':total_index<len(values),'blockers':blockers,'safe_to_cutover':not blockers,
            'operations':operations,'source_fingerprint':source_fingerprint,
            'canonical_fingerprint':canonical_fingerprint,'excluded_canonical':index['excluded'],
            'preserved_unbound_rows':preserved_unbound,'projected_rows':projected}

def build_cutover_dry_run(rows,summary,context=None):
    plan=plan_rows(rows,summary)
    context=dict(context or {})
    token=_digest({'contract_version':CUTOVER_CONTRACT,'context':context,
        'source_fingerprint':plan['source_fingerprint'],'canonical_fingerprint':plan['canonical_fingerprint']})
    return {'contract_version':CUTOVER_CONTRACT,'mode':'dry_run','plan_token':token,
            'source_fingerprint':plan['source_fingerprint'],'canonical_fingerprint':plan['canonical_fingerprint'],
            'safe_to_cutover':plan['safe_to_cutover'],'blockers':plan['blockers'],
            'guards':{'write_mode_unchanged':True,'unbound_legacy_rows_preserved':True,
                'bound_user_titles_preserved':True,'receipt_inference_forbidden':True,
                'excluded':['Готово боев','Монтаж 6 роликов POV']},
            'operations':plan['operations'],'preserved_unbound_rows':plan['preserved_unbound_rows'],
            'excluded_canonical':plan['excluded_canonical'],
            'legacy_unpaid_rub':str(plan['legacy_remaining']),
            'legacy_unpaid_excluded_rub':str(plan['legacy_unpaid_excluded']),
            'legacy_unpaid_requires_population_rub':str(plan['legacy_unpaid_requires_population']),
            'legacy_unpaid_excluded_rows':plan['legacy_unpaid_excluded_rows'],
            'legacy_unpaid_requires_population_rows':plan['legacy_unpaid_requires_population_rows'],
            'canonical_unpaid_rub':str(plan['canonical_remaining']),
            'total_unpaid_rub':str(plan['total']),**context}

def _read_configured_sheet(database,instance,credentials_path):
    configured=_load(database,instance)
    if configured is None:
        return None
    integration=configured['integration'];summary=configured['summary']
    spreadsheet_id=integration['spreadsheet_id'];sheet_name=integration.get('sheet_name','Лист1')
    client=GoogleSheets(credentials_path)
    metadata=client.metadata(spreadsheet_id)
    sheets=[s.get('properties') or {} for s in metadata.get('sheets') or []]
    matches=[s for s in sheets if s.get('title')==sheet_name]
    if len(matches)!=1:
        raise SheetSyncError('configured_google_sheet_tab_not_found')
    rows=client.values(spreadsheet_id,sheet_name)
    plan=plan_rows(rows,summary)
    return configured,client,matches[0]['sheetId'],rows,plan

def dry_run_configured(database,instance,credentials_path,workspace_id):
    loaded=_read_configured_sheet(database,instance,credentials_path)
    if loaded is None:
        return {'status':'disabled','reason':'no_active_rcc_sheet_integration'}
    configured,_client,_sheet_id,rows,_plan=loaded
    summary=configured['summary'];integration=configured['integration']
    if str(summary['workspace_id'])!=str(workspace_id):
        raise SheetSyncError('rcc_sheet_workspace_mismatch')
    result=build_cutover_dry_run(rows,summary,context={
        'workspace_id':summary['workspace_id'],'workspace_key':summary['workspace_key'],
        'spreadsheet_id':integration['spreadsheet_id'],'sheet_name':integration.get('sheet_name','Лист1'),
        'write_mode':integration.get('write_mode','observe')})
    result['status']='observe'
    return result

def sync_configured(database,instance,credentials_path):
    loaded=_read_configured_sheet(database,instance,credentials_path)
    if loaded is None:
        return {'status':'disabled','reason':'no_active_rcc_sheet_integration'}
    configured,client,sheet_id,_rows,plan=loaded
    integration=configured['integration'];summary=configured['summary']
    spreadsheet_id=integration['spreadsheet_id'];sheet_name=integration.get('sheet_name','Лист1')
    context={'workspace_id':summary['workspace_id'],'workspace_key':summary['workspace_key'],
        'spreadsheet_id':spreadsheet_id,'sheet_name':sheet_name,
        'write_mode':integration.get('write_mode','observe')}
    dry_run=build_cutover_dry_run(_rows,summary,context=context)
    result={'status':'observe' if integration.get('write_mode','observe')=='observe' else 'ok',
        'workspace_id':summary['workspace_id'],'spreadsheet_id':spreadsheet_id,'sheet_name':sheet_name,
        'managed_rows_rebuilt':len(plan['a_to_e']),'legacy_unpaid_rub':str(plan['legacy_remaining']),
        'legacy_unpaid_excluded_rub':str(plan['legacy_unpaid_excluded']),
        'legacy_unpaid_requires_population_rub':str(plan['legacy_unpaid_requires_population']),
        'canonical_unpaid_rub':str(plan['canonical_remaining']),'total_unpaid_rub':str(plan['total']),
        'projects':len(summary['projects']),'cutover_contract':CUTOVER_CONTRACT,
        'cutover_plan_token':dry_run['plan_token'],'safe_to_cutover':plan['safe_to_cutover'],
        'cutover_blockers':plan['blockers']}
    if integration.get('write_mode','observe')=='observe':
        return result
    if plan['blockers']:
        raise SheetSyncError('rcc_sheet_cutover_blocked')

    requests=[]
    for start,end in reversed(plan['delete_groups']):
        requests.append({'deleteDimension':{'range':{'sheetId':sheet_id,'dimension':'ROWS','startIndex':start,'endIndex':end}}})
    count=len(plan['a_to_e'])
    if count:
        requests.append({'insertDimension':{'range':{'sheetId':sheet_id,'dimension':'ROWS',
            'startIndex':plan['insert_at'],'endIndex':plan['insert_at']+count},'inheritFromBefore':plan['insert_at']>0}})
    requests.append({'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':6,'endIndex':10},
        'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}})
    if count:
        requests.append({'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':plan['insert_at'],
            'endRowIndex':plan['insert_at']+count,'startColumnIndex':2,'endColumnIndex':3},
            'cell':{'userEnteredFormat':{'numberFormat':{'type':'NUMBER','pattern':'#,##0 "₽"'}}},
            'fields':'userEnteredFormat.numberFormat'}})
        for offset,entity in enumerate(plan['entities']):
            row_index=plan['insert_at']+offset
            if entity['kind']=='project':
                requests.extend([
                    {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':row_index,'endRowIndex':row_index+1,
                        'startColumnIndex':0,'endColumnIndex':6},
                        'cell':{'userEnteredFormat':{'backgroundColor':PROJECT_BG}},
                        'fields':'userEnteredFormat.backgroundColor'}},
                    {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':row_index,'endRowIndex':row_index+1,
                        'startColumnIndex':0,'endColumnIndex':3},
                        'cell':{'userEnteredFormat':{'textFormat':{'foregroundColor':WHITE,'bold':True}}},
                        'fields':'userEnteredFormat.textFormat'}},
                    {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':row_index,'endRowIndex':row_index+1,
                        'startColumnIndex':3,'endColumnIndex':5},
                        'cell':{'userEnteredFormat':{'textFormat':{'foregroundColor':PROJECT_BG}}},
                        'fields':'userEnteredFormat.textFormat.foregroundColor'}},
                ])
            else:
                requests.extend([
                    {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':row_index,'endRowIndex':row_index+1,
                        'startColumnIndex':0,'endColumnIndex':3},
                        'cell':{'userEnteredFormat':{'backgroundColor':ITEM_BG,'textFormat':{'foregroundColor':DARK_TEXT}}},
                        'fields':'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor'}},
                    {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':row_index,'endRowIndex':row_index+1,
                        'startColumnIndex':3,'endColumnIndex':6},
                        'cell':{'userEnteredFormat':{'backgroundColor':WHITE}},
                        'fields':'userEnteredFormat.backgroundColor'}},
                ])
    total_zero_index=plan['insert_at']+count
    requests.extend([
        {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':total_zero_index,'endRowIndex':total_zero_index+1,
            'startColumnIndex':0,'endColumnIndex':6},
            'cell':{'userEnteredFormat':{'backgroundColor':PROJECT_BG}},
            'fields':'userEnteredFormat.backgroundColor'}},
        {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':total_zero_index,'endRowIndex':total_zero_index+1,
            'startColumnIndex':0,'endColumnIndex':3},
            'cell':{'userEnteredFormat':{'textFormat':{'foregroundColor':WHITE,'bold':True}}},
            'fields':'userEnteredFormat.textFormat'}},
    ])
    client.structural(spreadsheet_id,requests)

    start=plan['insert_at']+1
    end=start+count-1
    total_row=plan['insert_at']+count+1
    data=[{'range':f"'{sheet_name}'!G1:J1",'values':[METADATA_HEADER]}]
    if count:
        data.extend([
            {'range':f"'{sheet_name}'!A{start}:E{end}",'values':plan['a_to_e']},
            {'range':f"'{sheet_name}'!G{start}:J{end}",'values':plan['g_to_j']}])
    label_column=_column_name(plan['total_label_column'])
    amount_column=_column_name(plan['total_amount_column'])
    if plan['had_total']:
        data.append({'range':f"'{sheet_name}'!{amount_column}{total_row}",'values':[[float(plan['total'])]]})
    else:
        data.extend([
            {'range':f"'{sheet_name}'!{label_column}{total_row}",'values':[['Итого:']]},
            {'range':f"'{sheet_name}'!{amount_column}{total_row}",'values':[[float(plan['total'])]]},
        ])
    client.write_values(spreadsheet_id,data)
    return result
