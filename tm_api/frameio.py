"""Frame.io V4 read-only evidence adapter with server-only Adobe OAuth state."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import httpx
from .v24.common import Rejected

IMS_AUTHORIZE_URL="https://ims-na1.adobelogin.com/ims/authorize/v2"
IMS_TOKEN_URL="https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_API_BASE="https://api.frame.io/v4"
DEFAULT_SCOPES=("openid","profile","offline_access")
SAFE_ID=re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")
SAFE_CURSOR=re.compile(r"^[A-Za-z0-9._~+/=%:@?-]{1,4096}$")

class FrameIOError(RuntimeError):
    """Safe integration error; never includes upstream response bodies or credentials."""

class FrameIOOAuth:
    def __init__(self,*,enabled,client_id,client_secret_path,state_path,redirect_uri,
                 scopes=DEFAULT_SCOPES,clock=time.time,transport=None):
        self.enabled=bool(enabled)
        self.client_id=client_id
        self.client_secret_path=Path(client_secret_path) if client_secret_path else None
        self.state_path=Path(state_path) if state_path else None
        self.redirect_uri=redirect_uri
        self.scopes=tuple(scopes or DEFAULT_SCOPES)
        self.clock=clock
        self.transport=transport
        self.lock=threading.RLock()
        if self.enabled:
            if not client_id or len(client_id)>300:
                raise ValueError("Frame.io client id required")
            if not self.client_secret_path or not self.client_secret_path.is_absolute():
                raise ValueError("Frame.io client secret path must be absolute")
            if not self.state_path or not self.state_path.is_absolute():
                raise ValueError("Frame.io OAuth state path must be absolute")
            if not redirect_uri.startswith("https://"):
                raise ValueError("Frame.io redirect must use HTTPS")
            if "openid" not in self.scopes or "offline_access" not in self.scopes:
                raise ValueError("Frame.io OAuth requires openid and offline_access")

    def _blank(self):
        return {"access_token":None,"refresh_token":None,"expires_at":0,"scope":[],
                "pending_state_digest":None,"pending_state_expires_at":0,
                "reauthorization_required":False}

    def _load(self):
        if not self.enabled or not self.state_path or not self.state_path.exists():
            return self._blank()
        if self.state_path.is_symlink():
            raise FrameIOError("frameio_state_path_invalid")
        try:
            data=json.loads(self.state_path.read_text())
        except (OSError,ValueError,TypeError):
            raise FrameIOError("frameio_state_unavailable") from None
        if not isinstance(data,dict):
            raise FrameIOError("frameio_state_unavailable")
        return self._blank()|data

    def _save(self,data):
        if not self.state_path:
            raise FrameIOError("frameio_state_path_invalid")
        parent=self.state_path.parent
        parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if parent.is_symlink() or self.state_path.is_symlink():
            raise FrameIOError("frameio_state_path_invalid")
        os.chmod(parent,0o700)
        tmp=parent/(self.state_path.name+"."+secrets.token_hex(8)+".tmp")
        try:
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"w") as handle:
                handle.write(json.dumps(data,separators=(",",":"),sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp,self.state_path)
            os.chmod(self.state_path,0o600)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _secret(self):
        if not self.client_secret_path or not self.client_secret_path.exists():
            raise FrameIOError("frameio_client_secret_unavailable")
        if self.client_secret_path.is_symlink():
            raise FrameIOError("frameio_client_secret_path_invalid")
        try:
            value=self.client_secret_path.read_text().strip()
        except OSError:
            raise FrameIOError("frameio_client_secret_unavailable") from None
        if not 8<=len(value)<=4096 or "\x00" in value:
            raise FrameIOError("frameio_client_secret_invalid")
        return value

    def _http(self):
        return httpx.Client(timeout=httpx.Timeout(20.0,connect=10.0),follow_redirects=False,
                            transport=self.transport,headers={"User-Agent":"TelegramManager-FrameIO/1"})

    def status(self):
        if not self.enabled:
            return {"enabled":False,"authorized":False,"read_only_source":True,
                    "tokens_exposed":False,"reauthorization_required":False}
        with self.lock:
            data=self._load()
        return {"enabled":True,
                "authorized":bool(data.get("refresh_token") or (data.get("access_token") and
                                  float(data.get("expires_at") or 0)>self.clock())),
                "refresh_available":bool(data.get("refresh_token")),
                "access_expires_at":data.get("expires_at") or None,
                "reauthorization_required":bool(data.get("reauthorization_required")),
                "oauth_pending":bool(data.get("pending_state_digest") and
                                     float(data.get("pending_state_expires_at") or 0)>self.clock()),
                "redirect_uri":self.redirect_uri,"scopes":list(self.scopes),
                "read_only_source":True,"tokens_exposed":False}

    def begin(self):
        if not self.enabled:
            raise FrameIOError("frameio_disabled")
        state=secrets.token_urlsafe(32)
        with self.lock:
            data=self._load()
            data["pending_state_digest"]=hashlib.sha256(state.encode()).hexdigest()
            data["pending_state_expires_at"]=self.clock()+600
            data["reauthorization_required"]=False
            self._save(data)
        return IMS_AUTHORIZE_URL+"?"+urlencode({"client_id":self.client_id,
            "redirect_uri":self.redirect_uri,"scope":",".join(self.scopes),
            "state":state,"response_type":"code"})

    def _exchange(self,form):
        try:
            with self._http() as client:
                response=client.post(IMS_TOKEN_URL,data=form,
                    auth=httpx.BasicAuth(self.client_id,self._secret()),
                    headers={"Content-Type":"application/x-www-form-urlencoded"})
        except (httpx.HTTPError,OSError):
            raise FrameIOError("frameio_oauth_transport_unavailable") from None
        if response.status_code!=200:
            if response.status_code in (400,401,403):
                raise FrameIOError("frameio_oauth_rejected")
            raise FrameIOError("frameio_oauth_upstream_unavailable")
        try:
            payload=response.json()
        except ValueError:
            raise FrameIOError("frameio_oauth_invalid_response") from None
        access=payload.get("access_token")
        refresh=payload.get("refresh_token")
        try:
            expires=int(payload.get("expires_in"))
        except (TypeError,ValueError):
            raise FrameIOError("frameio_oauth_invalid_response") from None
        if not isinstance(access,str) or not 20<=len(access)<=16384 or not 60<=expires<=172800:
            raise FrameIOError("frameio_oauth_invalid_response")
        if refresh is not None and (not isinstance(refresh,str) or not 20<=len(refresh)<=16384):
            raise FrameIOError("frameio_oauth_invalid_response")
        return {"access_token":access,"refresh_token":refresh,
                "expires_at":self.clock()+expires,
                "scope":str(payload.get("scope") or "").replace(","," ").split()}

    def complete(self,code,state):
        if not self.enabled:
            raise FrameIOError("frameio_disabled")
        if not isinstance(code,str) or not 8<=len(code)<=8192:
            raise FrameIOError("frameio_oauth_invalid_callback")
        if not isinstance(state,str) or not 16<=len(state)<=1024:
            raise FrameIOError("frameio_oauth_invalid_callback")
        digest=hashlib.sha256(state.encode()).hexdigest()
        with self.lock:
            data=self._load()
            expected=data.get("pending_state_digest")
            if not expected or not secrets.compare_digest(expected,digest):
                raise FrameIOError("frameio_oauth_state_mismatch")
            if float(data.get("pending_state_expires_at") or 0)<=self.clock():
                raise FrameIOError("frameio_oauth_state_mismatch")
            token=self._exchange({"code":code,"grant_type":"authorization_code",
                                  "redirect_uri":self.redirect_uri})
            if not token.get("refresh_token"):
                raise FrameIOError("frameio_oauth_refresh_missing")
            data.update(token)
            data["pending_state_digest"]=None
            data["pending_state_expires_at"]=0
            data["reauthorization_required"]=False
            self._save(data)
        return {"authorized":True,"read_only_source":True,"tokens_exposed":False}

    def access_token(self):
        if not self.enabled:
            raise FrameIOError("frameio_disabled")
        with self.lock:
            data=self._load()
            if data.get("access_token") and float(data.get("expires_at") or 0)>self.clock()+90:
                return data["access_token"]
            refresh=data.get("refresh_token")
            if not refresh:
                raise FrameIOError("frameio_authorization_required")
            try:
                token=self._exchange({"grant_type":"refresh_token","refresh_token":refresh})
            except FrameIOError as exc:
                if str(exc)=="frameio_oauth_rejected":
                    data["reauthorization_required"]=True
                    data["access_token"]=None
                    data["expires_at"]=0
                    self._save(data)
                raise
            token["refresh_token"]=token.get("refresh_token") or refresh
            data.update(token)
            data["reauthorization_required"]=False
            self._save(data)
            return data["access_token"]

def _id(value,label="id"):
    if not isinstance(value,str) or not SAFE_ID.fullmatch(value):
        raise Rejected("invalid_frameio_"+label)
    return value

def _cursor(value):
    if value is None:
        return None
    if not isinstance(value,str) or not SAFE_CURSOR.fullmatch(value):
        raise Rejected("invalid_frameio_cursor")
    return value

def _pick(data,keys):
    return {key:data.get(key) for key in keys if isinstance(data,dict) and key in data}

def _next_cursor(payload):
    links=payload.get("links") if isinstance(payload,dict) else None
    nxt=links.get("next") if isinstance(links,dict) else None
    if not isinstance(nxt,str) or not nxt:
        return None
    try:
        query=parse_qs(urlsplit(nxt).query)
    except ValueError:
        return None
    for key in ("after","before","cursor","page[after]","page[before]"):
        values=query.get(key)
        if values and isinstance(values[0],str) and len(values[0])<=4096:
            return values[0]
    return None

def _list_data(payload):
    data=payload.get("data") if isinstance(payload,dict) else None
    return data if isinstance(data,list) else []

def _one_data(payload):
    data=payload.get("data") if isinstance(payload,dict) else None
    return data if isinstance(data,dict) else {}

ACCOUNT_FIELDS=("id","name","display_name","created_at","updated_at")
WORKSPACE_FIELDS=("id","name","account_id","created_at","updated_at")
PROJECT_FIELDS=("id","name","workspace_id","root_folder_id","created_at","updated_at")
ASSET_FIELDS=("id","adobe_id","adobe_version_id","name","type","parent_id","project_id",
              "media_type","file_size","status","created_at","updated_at","version_stack_id")
STACK_FIELDS=("id","name","parent_id","project_id","created_at","updated_at")
COMMENT_FIELDS=("id","file_id","text","timestamp","page","duration",
                "completed_at","created_at","updated_at","text_edited_at")

def _stack(data):
    value=_pick(data,STACK_FIELDS)
    head=data.get("head_version") if isinstance(data,dict) else None
    if isinstance(head,dict):
        value["head_version"]=_pick(head,ASSET_FIELDS)
    return value

class FrameIOReader:
    def __init__(self,oauth,transport=None):
        self.oauth=oauth
        self.transport=transport

    def status(self):
        return self.oauth.status()

    def _http(self):
        return httpx.Client(base_url=FRAMEIO_API_BASE,
            timeout=httpx.Timeout(20.0,connect=10.0),follow_redirects=False,
            transport=self.transport,headers={"Authorization":"Bearer "+self.oauth.access_token(),
            "Accept":"application/json","User-Agent":"TelegramManager-FrameIO/1"})

    def _request(self,method,path,*,params=None,json_body=None):
        if method not in ("GET","POST"):
            raise FrameIOError("frameio_method_not_allowed")
        if method=="POST" and not re.fullmatch(r"/accounts/[^/]+/search",path):
            raise FrameIOError("frameio_write_not_allowed")
        try:
            with self._http() as client:
                response=client.request(method,path,params=params,json=json_body)
        except (httpx.HTTPError,OSError):
            raise FrameIOError("frameio_api_unavailable") from None
        if response.status_code==401:
            raise FrameIOError("frameio_authorization_required")
        if response.status_code==403:
            raise FrameIOError("frameio_forbidden")
        if response.status_code==404:
            raise Rejected("frameio_not_found")
        if response.status_code==429:
            raise FrameIOError("frameio_rate_limited")
        if response.status_code>=400:
            raise FrameIOError("frameio_api_error")
        try:
            payload=response.json()
        except ValueError:
            raise FrameIOError("frameio_invalid_response") from None
        if not isinstance(payload,dict):
            raise FrameIOError("frameio_invalid_response")
        return payload

    def _page(self,path,fields,limit=50,cursor=None,*,params=None,projector=None):
        if type(limit) is not int or not 1<=limit<=100:
            raise Rejected("invalid_frameio_limit")
        cursor=_cursor(cursor)
        query={"page_size":limit}
        if cursor:
            query["after"]=cursor
        if params:
            query.update(params)
        payload=self._request("GET",path,params=query)
        next_cursor=_next_cursor(payload)
        items=[projector(item) if projector else _pick(item,fields) for item in _list_data(payload)]
        return {"items":items,
                "next_cursor":next_cursor,"has_more":next_cursor is not None,
                "read_only_source":True,"source_content_is_untrusted":True}

    def accounts(self,limit=50,cursor=None):
        return self._page("/accounts",ACCOUNT_FIELDS,limit,cursor)

    def workspaces(self,account_id,limit=50,cursor=None):
        account_id=_id(account_id,"account_id")
        return self._page(f"/accounts/{account_id}/workspaces",WORKSPACE_FIELDS,limit,cursor)

    def projects(self,account_id,workspace_id,limit=50,cursor=None):
        account_id=_id(account_id,"account_id")
        workspace_id=_id(workspace_id,"workspace_id")
        return self._page(f"/accounts/{account_id}/workspaces/{workspace_id}/projects",
                          PROJECT_FIELDS,limit,cursor)

    def project(self,account_id,project_id):
        account_id=_id(account_id,"account_id")
        project_id=_id(project_id,"project_id")
        data=_one_data(self._request("GET",f"/accounts/{account_id}/projects/{project_id}"))
        return {"project":_pick(data,PROJECT_FIELDS),"read_only_source":True}

    def folder(self,account_id,folder_id):
        account_id=_id(account_id,"account_id")
        folder_id=_id(folder_id,"folder_id")
        data=_one_data(self._request("GET",f"/accounts/{account_id}/folders/{folder_id}"))
        return {"folder":_pick(data,ASSET_FIELDS),"read_only_source":True}

    def folder_children(self,account_id,folder_id,limit=50,cursor=None):
        account_id=_id(account_id,"account_id")
        folder_id=_id(folder_id,"folder_id")
        return self._page(f"/accounts/{account_id}/folders/{folder_id}/children",
                          ASSET_FIELDS,limit,cursor)

    def file(self,account_id,file_id):
        account_id=_id(account_id,"account_id")
        file_id=_id(file_id,"file_id")
        data=_one_data(self._request("GET",f"/accounts/{account_id}/files/{file_id}"))
        return {"file":_pick(data,ASSET_FIELDS),"read_only_source":True}

    def version_stacks(self,account_id,folder_id,limit=50,cursor=None):
        account_id=_id(account_id,"account_id")
        folder_id=_id(folder_id,"folder_id")
        return self._page(f"/accounts/{account_id}/folders/{folder_id}/version_stacks",
                          STACK_FIELDS,limit,cursor,projector=_stack)

    def version_stack(self,account_id,version_stack_id,max_versions=100):
        account_id=_id(account_id,"account_id")
        version_stack_id=_id(version_stack_id,"version_stack_id")
        if type(max_versions) is not int or not 1<=max_versions<=100:
            raise Rejected("invalid_frameio_limit")
        stack=_one_data(self._request("GET",f"/accounts/{account_id}/version_stacks/{version_stack_id}"))
        payload=self._request("GET",
            f"/accounts/{account_id}/version_stacks/{version_stack_id}/children",
            params={"page_size":max_versions})
        versions=[]
        for position,item in enumerate(_list_data(payload),start=1):
            value=_pick(item,ASSET_FIELDS)
            value["stack_position"]=position
            versions.append(value)
        next_cursor=_next_cursor(payload)
        safe_stack=_stack(stack)
        current=safe_stack.get("head_version")
        return {"version_stack":safe_stack,"versions":versions,
                "current_version":current,"current_version_exact":bool(current),
                "children_complete":next_cursor is None,"truncated":next_cursor is not None,
                "version_order":"frameio_api_order",
                "read_only_source":True,"source_content_is_untrusted":True}

    def comments(self,account_id,file_id,limit=50,cursor=None):
        account_id=_id(account_id,"account_id")
        file_id=_id(file_id,"file_id")
        if type(limit) is not int or not 1<=limit<=100:
            raise Rejected("invalid_frameio_limit")
        cursor=_cursor(cursor)
        params={"page_size":limit,"include":"owner","timestamp_as_timecode":"true"}
        if cursor:
            params["after"]=cursor
        payload=self._request("GET",f"/accounts/{account_id}/files/{file_id}/comments",params=params)
        items=[]
        for raw in _list_data(payload):
            value=_pick(raw,COMMENT_FIELDS)
            owner=raw.get("owner") if isinstance(raw,dict) else None
            if isinstance(owner,dict):
                value["author"]=_pick(owner,("id","name","adobe_user_id"))
            items.append(value)
        next_cursor=_next_cursor(payload)
        return {"items":items,"next_cursor":next_cursor,"has_more":next_cursor is not None,
                "timestamp_format":"frameio_timecode_HH:MM:SS:FF",
                "read_only_source":True,"source_content_is_untrusted":True}

    def search(self,account_id,query,limit=25,cursor=None,engine="lexical"):
        account_id=_id(account_id,"account_id")
        if not isinstance(query,str) or not 1<=len(query.strip())<=500 or "\x00" in query:
            raise Rejected("invalid_frameio_query")
        if engine not in ("lexical","nlp"):
            raise Rejected("invalid_frameio_search_engine")
        if type(limit) is not int or not 1<=limit<=50:
            raise Rejected("invalid_frameio_limit")
        cursor=_cursor(cursor)
        params={"page_size":limit}
        if cursor:
            params["after"]=cursor
        body={"query":query.strip(),"engine":engine,
              "filters":{"files_and_version_stacks":True,"folders":True,"projects":True}}
        payload=self._request("POST",f"/accounts/{account_id}/search",
                              params=params,json_body=body)
        items=[]
        for raw in _list_data(payload):
            if not isinstance(raw,dict):
                continue
            result=raw.get("result") if isinstance(raw.get("result"),dict) else raw
            value=_pick(result,ASSET_FIELDS+PROJECT_FIELDS+STACK_FIELDS)
            matches=raw.get("matches")
            if isinstance(matches,list):
                value["matches"]=[_pick(m,("type","name","text","terms","timestamp",
                                           "start_time","end_time"))
                                  for m in matches[:20] if isinstance(m,dict)]
            items.append(value)
        next_cursor=_next_cursor(payload)
        return {"items":items,"next_cursor":next_cursor,"has_more":next_cursor is not None,
                "coverage":{"bounded":True,"max_results_per_page":50,
                            "history_complete":False},
                "read_only_source":True,"source_content_is_untrusted":True}

def bind_frameio_project_to_umbrella(umbrellas,project,files):
    """Bind project identity once; child Frame.io file identities inherit the same umbrella."""
    from .v25.reading import umbrella_binding_hint
    project_id=str((project or {}).get("id") or "")
    project_name=str((project or {}).get("name") or "")
    binding=umbrella_binding_hint(umbrellas or [],project_name)
    entity_id=binding.get("entity_id") if binding.get("status")=="bind_existing" else None
    assets=[{"frameio_project_id":project_id,"frameio_file_id":item.get("id"),
             "frameio_file_name":item.get("name"),"umbrella_entity_id":entity_id}
            for item in files or [] if isinstance(item,dict)]
    return {"frameio_project":{"id":project_id,"name":project_name},
            "umbrella_binding":binding,"assets":assets,"creates_umbrella":False,
            "source_role":"supplemental_evidence"}
