#!/usr/bin/env python3
# server/invoices_server.py
"""casa invoices MCP server. Stdlib-only stdio JSON-RPC.

This file only dispatches; every behaviour lives in a focused module
(store, model, numbering, invoice, email_template, pdf, invoice_pdf,
finalize) and the tools are registered by tools.py.
"""
from __future__ import annotations

import json
import pathlib
import sys

import store

#: name -> {"description": str, "schema": {...}, "fn": callable, "writes": bool}
TOOLS: dict = {}
#: Exception types whose message is written for the operator; tools.py fills it.
#: They render as "error: <message>", anything else as "error: <Type>: <message>".
USER_ERRORS: list = []
PROTOCOL_VERSION = "2024-11-05"
_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
VERSION = json.loads(_MANIFEST.read_text(encoding="utf-8"))["version"]


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _call(tool: dict, args: dict):
    """Run the tool; a writing tool holds the write lock for its whole body
    (the lock is not reentrant, so nothing below takes it again)."""
    if tool["writes"]:
        with store.write_lock():
            return tool["fn"](args)
    return tool["fn"](args)


def handle(req: dict) -> dict | None:
    method, id_ = req.get("method"), req.get("id")
    if method == "initialize":
        return _result(id_, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "invoices", "version": VERSION}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["schema"]}
            for n, t in sorted(TOOLS.items())]})
    if method == "tools/call":
        params = req.get("params") or {}
        tool = TOOLS.get(params.get("name"))
        if tool is None:
            return _error(id_, -32601, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments") or {}
        try:
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            out = _call(tool, args)
        except Exception as exc:                     # surfaced, never swallowed
            if isinstance(exc, tuple(USER_ERRORS)):
                text = f"error: {exc}"
            else:
                text = f"error: {type(exc).__name__}: {exc}"
            return _result(id_, {"content": [{"type": "text", "text": text}],
                                 "isError": True})
        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False, indent=2)
        return _result(id_, {"content": [{"type": "text", "text": out}]})
    return _error(id_, -32601, f"unknown method {method!r}")


def main() -> None:
    # Launched as a script this module is "__main__"; tools.py does
    # `import invoices_server` to reach TOOLS. Without the alias that import
    # would run this file again as a second module with an empty TOOLS dict,
    # and handle() would answer tools/list with nothing (bank-feed's reason).
    sys.modules.setdefault("invoices_server", sys.modules[__name__])
    import tools  # noqa: F401  (registers every tool; a failure here is fatal)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # bad JSON, a too-long number, too deep (RecursionError)
            continue       # no id can be recovered from an unparsed line: skip
        if not isinstance(req, dict):
            continue
        try:
            resp = handle(req)
        except Exception as exc:                     # e.g. "params": [1]; never exit
            resp = _error(req.get("id"), -32603,
                          f"internal error: {type(exc).__name__}: {exc}")
        if resp is not None and "id" in req:         # notifications get no reply
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
