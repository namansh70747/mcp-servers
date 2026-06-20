"""Tiny structured stderr logger (stdout is reserved for the MCP stdio protocol).

    from mcp_base import get_logger
    log = get_logger("funding-radar")
    log.info("scanned", source="techcrunch", n=12)
"""
from __future__ import annotations

import logging
import os
import sys

_LEVEL = os.environ.get("MCP_LOG_LEVEL", "WARNING").upper()
_configured: set[str] = set()


class _KVFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname} {record.name}: {record.getMessage()}"
        extra = getattr(record, "_kv", None)
        if extra:
            base += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


class _KVLogger(logging.LoggerAdapter):
    def log(self, level, msg, *args, **kwargs):  # type: ignore[override]
        kv = {k: v for k, v in kwargs.items() if k not in ("exc_info", "stack_info", "stacklevel")}
        for k in list(kv):
            kwargs.pop(k, None)
        self.logger.log(level, msg, *args, extra={"_kv": kv}, **kwargs)


def get_logger(name: str) -> _KVLogger:
    """Return a logger that writes `LEVEL name: msg k=v` to stderr. Level via MCP_LOG_LEVEL."""
    logger = logging.getLogger(f"mcp.{name}")
    if name not in _configured:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(_KVFormatter())
        logger.addHandler(h)
        logger.setLevel(_LEVEL)
        logger.propagate = False
        _configured.add(name)
    return _KVLogger(logger, {})
