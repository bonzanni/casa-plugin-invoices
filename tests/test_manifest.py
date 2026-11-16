# tests/test_manifest.py
"""The manifest declares every tool, and protected summaries name real arguments."""
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
import invoices_server  # noqa: E402
import tools  # noqa: E402,F401  (registers into invoices_server.TOOLS)

MANIFEST = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
PROTECTED = {"invoice_finalize", "invoice_discard", "invoice_customer_remove"}


class TestManifest(unittest.TestCase):
    def test_fourteen_tools(self):
        self.assertEqual(len(invoices_server.TOOLS), 14)

    def test_every_tool_declared_in_result_contract(self):
        contract = MANIFEST["casa"]["resultContract"]
        self.assertEqual(contract["version"], 1)
        self.assertEqual(set(contract["tools"]), set(invoices_server.TOOLS))
        for name, entry in contract["tools"].items():
            self.assertEqual(entry, {"result": "safe"}, name)

    def test_protected_tools_are_exactly_the_three(self):
        names = {p["name"] for p in MANIFEST["casa"]["protectedTools"]}
        self.assertEqual(names, PROTECTED)

    def test_protected_placeholders_are_arguments(self):
        for entry in MANIFEST["casa"]["protectedTools"]:
            props = invoices_server.TOOLS[entry["name"]]["schema"].get("properties", {})
            for placeholder in re.findall(r"\{([^{}]*)\}", entry["summary"]):
                self.assertIn(placeholder, props, entry["name"])
                self.assertIn(placeholder,
                              invoices_server.TOOLS[entry["name"]]["schema"].get("required", []),
                              entry["name"])

    def test_writes_flags(self):
        writers = {n for n, t in invoices_server.TOOLS.items() if t["writes"]}
        self.assertEqual(writers, {
            "invoice_company_set", "invoice_customer_set", "invoice_customer_remove",
            "invoice_new", "invoice_edit", "invoice_discard", "invoice_finalize"})

    def test_mcp_json_points_at_server(self):
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["invoices"]
        self.assertEqual(server["args"], ["${CLAUDE_PLUGIN_ROOT}/server/invoices_server.py"])
        self.assertTrue((SERVER / "invoices_server.py").is_file())

    def test_server_version_is_manifest_version(self):
        resp = invoices_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        info = resp["result"]["serverInfo"]
        self.assertEqual(info, {"name": "invoices", "version": MANIFEST["version"]})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")


if __name__ == "__main__":
    unittest.main()
