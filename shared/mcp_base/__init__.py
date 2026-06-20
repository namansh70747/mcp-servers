"""Shared foundation for every custom server in the suite.

Import surface kept tiny on purpose:
    from mcp_base import make_server, BaseStore, data_dir, db_path, get_env
"""
from . import http
from .app import make_server
from .config import (
    base_data_dir,
    data_dir,
    db_path,
    get_env,
    get_env_bool,
    get_env_int,
    load_repo_env,
    repo_root,
)
from .errors import err, not_found, ok
from .gmail import DEFAULT_GMAIL_SCOPES, get_gmail_service, gmail_auth_status
from .log import get_logger
from .store import BaseStore

__all__ = [
    "make_server",
    "BaseStore",
    "base_data_dir",
    "data_dir",
    "db_path",
    "get_env",
    "get_env_bool",
    "get_env_int",
    "load_repo_env",
    "repo_root",
    "get_gmail_service",
    "gmail_auth_status",
    "DEFAULT_GMAIL_SCOPES",
    "ok",
    "err",
    "not_found",
    "get_logger",
    "http",
]
