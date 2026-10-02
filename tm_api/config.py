import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    database_url: str
    database_token: str
    token_sha256: str
    instance_id: str = "macbook-owner"
    allowed_hosts: tuple = ("127.0.0.1", "localhost")
    allowed_origins: tuple = ()
    audit_path: str = "state/audit.sqlite"
    requests_per_minute: int = 60
    writes_enabled: bool = False
    background_enabled: bool = False
    jobs_path: str = "state/jobs.sqlite"
    sync_token_sha256: str = ""
    oauth_enabled: bool = False
    public_url: str = "https://service.example.invalid/telegram-manager"
    oauth_path: str = "state/oauth.json"
    configurable_enabled: bool = False
    dynamic_enabled: bool = False
    v24_dsn: str = field(default="", repr=False)
    google_sheets_credentials_path: str = field(default="", repr=False)
    whatsapp_db_path: str = "/state/whatsapp/messages.sqlite"
    frameio_enabled: bool = False
    frameio_client_id: str = ""
    frameio_client_secret_path: str = field(default="", repr=False)
    frameio_state_path: str = "/state/frameio/oauth.json"
    frameio_redirect_uri: str = ""
    frameio_scopes: tuple = ("openid", "profile", "offline_access")

    @classmethod
    def from_env(cls):
        public_url = os.environ.get("TM_PUBLIC_URL", "https://service.example.invalid/telegram-manager")
        value = cls(
            database_url=os.environ["TM_DATABASE_REST_URL"].rstrip("/"),
            database_token=os.environ["TM_DATABASE_TOKEN"],
            token_sha256=os.environ["TM_API_TOKEN_SHA256"],
            instance_id=os.environ.get("TM_INSTANCE_ID", "macbook-owner"),
            allowed_hosts=tuple(os.environ.get("TM_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")),
            allowed_origins=tuple(filter(None, os.environ.get("TM_ALLOWED_ORIGINS", "").split(","))),
            audit_path=os.environ.get("TM_AUDIT_PATH", "state/audit.sqlite"),
            writes_enabled=os.environ.get("TM_WRITES_ENABLED", "false") == "true",
            background_enabled=os.environ.get("TM_BACKGROUND_ENABLED", "false") == "true",
            jobs_path=os.environ.get("TM_JOBS_PATH", "state/jobs.sqlite"),
            sync_token_sha256=os.environ.get("TM_SYNC_TOKEN_SHA256", ""),
            oauth_enabled=os.environ.get("TM_OAUTH_ENABLED", "false") == "true",
            oauth_path=os.environ.get("TM_OAUTH_PATH", "state/oauth.json"),
            public_url=public_url,
            configurable_enabled=os.environ.get("TM_CONFIGURABLE_ENABLED", "false") == "true",
            dynamic_enabled=os.environ.get("TM_DYNAMIC_ENABLED", "false") == "true",
            v24_dsn=os.environ.get("TM_V24_DSN", ""),
            google_sheets_credentials_path=os.environ.get("TM_GOOGLE_SHEETS_CREDENTIALS", ""),
            whatsapp_db_path=os.environ.get("TM_WHATSAPP_DB_PATH", "/state/whatsapp/messages.sqlite"),
            frameio_enabled=os.environ.get("TM_FRAMEIO_ENABLED", "false") == "true",
            frameio_client_id=os.environ.get("TM_FRAMEIO_CLIENT_ID", ""),
            frameio_client_secret_path=os.environ.get("TM_FRAMEIO_CLIENT_SECRET_PATH", ""),
            frameio_state_path=os.environ.get("TM_FRAMEIO_STATE_PATH", "/state/frameio/oauth.json"),
            frameio_redirect_uri=os.environ.get("TM_FRAMEIO_REDIRECT_URI",
                                                public_url.rstrip("/") + "/frameio/oauth/callback"),
            frameio_scopes=tuple(filter(None, os.environ.get(
                "TM_FRAMEIO_SCOPES", "openid,profile,offline_access").split(","))),
        )
        url = urlsplit(value.database_url)
        if (url.scheme != "http" or url.hostname != "db-rest"
                or url.path or url.query or url.fragment or url.username or url.password or url.port != 3000):
            raise ValueError("Unexpected database target")
        if not re.fullmatch(r"[a-f0-9]{64}", value.token_sha256):
            raise ValueError("API token digest required")
        if hashlib.sha256(value.database_token.encode()).hexdigest() == value.token_sha256:
            raise ValueError("API and database credentials must differ")
        if value.instance_id != "macbook-owner" or "*" in value.allowed_hosts:
            raise ValueError("Explicit personal scope required")
        if value.sync_token_sha256 and (not re.fullmatch(r"[a-f0-9]{64}", value.sync_token_sha256) or value.sync_token_sha256 == value.token_sha256):
            raise ValueError("Separate Mac credential required")
        if value.configurable_enabled and not value.dynamic_enabled:
            raise ValueError("V25 requires the dynamic API")
        if value.dynamic_enabled and not value.v24_dsn:
            raise ValueError("Private V24 database credential required")
        if value.google_sheets_credentials_path and not os.path.isabs(value.google_sheets_credentials_path):
            raise ValueError("Google Sheets credential path must be absolute")
        if not os.path.isabs(value.whatsapp_db_path):
            raise ValueError("WhatsApp store path must be absolute")
        if value.frameio_enabled:
            if not value.frameio_client_id:
                raise ValueError("Frame.io client id required")
            if not os.path.isabs(value.frameio_client_secret_path):
                raise ValueError("Frame.io client secret path must be absolute")
            if not os.path.isabs(value.frameio_state_path):
                raise ValueError("Frame.io OAuth state path must be absolute")
            if not value.frameio_redirect_uri.startswith("https://"):
                raise ValueError("Frame.io OAuth redirect must use HTTPS")
            if "openid" not in value.frameio_scopes or "offline_access" not in value.frameio_scopes:
                raise ValueError("Frame.io OAuth scopes require openid and offline_access")
        if not value.public_url.startswith("https://"):
            raise ValueError("HTTPS public URL required")
        return value
