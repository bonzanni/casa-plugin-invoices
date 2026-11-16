#!/usr/bin/env python3
# tests/e2e_smoke.py
"""End-to-end smoke: the real server as a subprocess over stdio.

initialize -> tools/list (14) -> setup -> new -> preview -> finalize, then
checks the published PDF in the temp handoff dir. Run: python3.11 tests/e2e_smoke.py
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "invoices_server.py"


class Server:
    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, env=env, text=True)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        resp = json.loads(self.proc.stdout.readline())
        assert resp["id"] == self.next_id, resp
        return resp

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, name, args):
        result = self.request("tools/call", {"name": name, "arguments": args})["result"]
        text = result["content"][0]["text"]
        assert not result.get("isError"), f"{name}: {text}"
        print(f"  {name}: ok")
        return text

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="invoices-smoke-")
    try:
        data, handoff, outbox = (os.path.join(tmp, d) for d in ("data", "handoff", "outbox"))
        os.mkdir(handoff)
        os.mkdir(outbox)
        env = {**os.environ, "CLAUDE_PLUGIN_DATA": data, "CASA_HANDOFF_DIR": handoff,
               "CASA_PLUGIN_OUTBOX_DIR": outbox}
        srv = Server(env)
        try:
            init = srv.request("initialize", {"protocolVersion": "2024-11-05",
                                              "capabilities": {}, "clientInfo": {"name": "smoke"}})
            print("initialize:", init["result"]["serverInfo"])
            srv.notify("notifications/initialized")
            listed = srv.request("tools/list")["result"]["tools"]
            assert len(listed) == 14, [t["name"] for t in listed]
            print(f"tools/list: {len(listed)} tools")

            srv.call("invoice_company_set", {"fields": {
                "name": "Example Holding B.V.", "address_lines": ["Voorbeeldstraat 1",
                                                                  "1234 AB Amsterdam"],
                "country": "Netherlands", "email": "billing@example.com",
                "iban": "NL00BANK0417164300", "bank": "Example Bank", "tax_name": "BTW"},
                "last_invoice_number": "INV-2026-0011"})
            srv.call("invoice_customer_set", {"customer": "acme", "fields": {
                "name": "Acme Biotech B.V.", "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
                "country": "Netherlands", "email": {"to": ["ap@example.com"],
                                                    "cc": ["inbox@example.com"]}}})
            summary = srv.call("invoice_new", {"customer": "acme", "period": "2026-12",
                                               "invoice_date": "2026-12-31", "items": [
                {"description": "Management fee", "qty": "1", "unit_price": "8400.00"},
                {"description": "Travel expenses", "qty": "1", "unit_price": "300.00"}]})
            print(summary)
            draft = re.search(r"^Draft: (\S+)$", summary, re.M).group(1)
            ref = re.search(r"^Ref: (\S+)$", summary, re.M).group(1)

            preview = srv.call("invoice_preview", {"draft_id": draft})
            ppath = preview.removeprefix("Path: ")
            assert os.path.dirname(ppath) == outbox and os.path.isfile(ppath), preview
            print("preview:", ppath)

            pkg = json.loads(srv.call("invoice_finalize", {"draft_id": draft,
                                                           "customer": "Acme Biotech B.V.",
                                                           "approval_ref": ref}))
            print("package:", json.dumps(pkg, indent=2, ensure_ascii=False))
            assert pkg["invoice_number"] == "INV-2026-0012", pkg
            assert pkg["total"] == "10527.00", pkg
            rel = os.path.relpath(pkg["pdf_path"], handoff)
            assert re.fullmatch(r"invoices/[0-9]{13}-[0-9a-f]{16}/INV-2026-0012\.pdf", rel), rel
            with open(pkg["pdf_path"], "rb") as f:
                assert f.read(5) == b"%PDF-", "not a PDF"
        finally:
            srv.close()
        print("SMOKE OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
