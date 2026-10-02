"""Restricted operator bridge for an explicitly approved workspace storage cutover.

Input is JSON on stdin. Only the versioned migrate_workspace_storage mutation
and exact preview application are exposed; there is no arbitrary SQL/RPC path.
"""
import json
import sys

from .config import Config
from .v24.common import Rejected
from .v24.database import Database
from .v24.engine import Engine
from .v24.models import Mutation


def load():
    try:
        value=json.load(sys.stdin)
    except Exception as exc:
        raise Rejected("invalid_storage_migration_input") from exc
    if not isinstance(value,dict):
        raise Rejected("invalid_storage_migration_input")
    return value


def runtime():
    config=Config.from_env()
    if not config.v24_dsn:
        raise Rejected("dynamic_database_not_configured")
    return Engine(Database(config.v24_dsn),config.instance_id,configurable=True)


def preview(engine,data):
    key=data.get("request_key")
    raw=data.get("mutations")
    if not isinstance(key,str) or not isinstance(raw,list) or not 1<=len(raw)<=20:
        raise Rejected("invalid_storage_migration_preview")
    mutations=[]
    for item in raw:
        mutation=Mutation.model_validate(item)
        if mutation.operation!="migrate_workspace_storage":
            raise Rejected("storage_migration_operation_only")
        mutations.append(mutation)
    return engine.preview(key,mutations)


def apply(engine,data):
    allowed={"request_key","preview_id","preview_digest","confirmation_ref"}
    if set(data)!=allowed:
        raise Rejected("invalid_storage_migration_apply")
    return engine.apply(
        data["request_key"],data["preview_id"],data["preview_digest"],True,
        data["confirmation_ref"])


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ("preview","apply"):
        raise SystemExit("usage: python -m tm_api.storage_migration_cli preview|apply")
    engine=runtime();data=load()
    result=preview(engine,data) if sys.argv[1]=="preview" else apply(engine,data)
    print(json.dumps(result,ensure_ascii=False,default=str,separators=(",",":")))


if __name__=="__main__":
    main()
