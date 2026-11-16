#!/usr/bin/env python3
"""Three-way tool-list agreement: the server's registry, plugin.json
casa.provides_tools and casa.resultContract.tools name exactly the same tools;
every result contract entry is {"result": "safe"} (the tools return drafts,
summaries and packages, never a credential or link); every protected tool is
registered."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "mcp__plugin_invoices_invoices__"


def main() -> int:
    sys.path.insert(0, str(ROOT / "server"))
    import invoices_server  # noqa: E402
    sys.modules.setdefault("invoices_server", invoices_server)
    import tools  # noqa: F401,E402
    server = set(invoices_server.TOOLS)
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text("utf-8"))
    casa = manifest["casa"]
    provided_raw = casa.get("provides_tools", [])
    problems = [f"provides_tools entry without the plugin prefix: {t}"
                for t in provided_raw if not t.startswith(PREFIX)]
    provided = {t[len(PREFIX):] for t in provided_raw if t.startswith(PREFIX)}
    if len(provided) != len(provided_raw):
        problems.append("provides_tools lists a tool twice or with a foreign prefix")
    contract = set(casa["resultContract"]["tools"])
    for name, other in (("provides_tools", provided), ("resultContract", contract)):
        for t in sorted(server - other):
            problems.append(f"server registers {t} but {name} does not list it")
        for t in sorted(other - server):
            problems.append(f"{name} lists {t} but the server does not register it")
    for t, c in casa["resultContract"]["tools"].items():
        if c != {"result": "safe"}:
            problems.append(f"resultContract for {t} must be {{'result': 'safe'}}")
    for t in casa.get("protectedTools", []):
        if t.get("name") not in server:
            problems.append(f"protectedTools names {t.get('name')}, which is not registered")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
