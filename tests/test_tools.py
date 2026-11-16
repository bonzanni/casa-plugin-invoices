# tests/test_tools.py
"""The 14 tools through the JSON-RPC dispatcher, on a temp data dir."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
import finalize  # noqa: E402
import invoices_server  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402,F401

COMPANY = {
    "name": "Example Holding B.V.",
    "address_lines": ["Keizersgracht 1", "1015 CJ Amsterdam"],
    "country": "Netherlands",
    "vat_id": "NL123456789B01",
    "email": "invoices@example.com",
    "iban": "NL00BANK0417164300",
    "bank": "ABN AMRO",
    "tax_name": "BTW",
}
CUSTOMER = {
    "name": "Acme Biotech B.V.",
    "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
    "country": "Netherlands",
    "email": {"to": ["ap@example.com"], "cc": ["books@example.com"]},
}
ITEMS = [
    {"description": "Management fee", "qty": "1", "unit_price": "8400.00"},
    {"description": "Travel expenses", "qty": "2", "unit_price": "178.92"},
]
LEGACY = {
    "invoice_number": "INV-2026-0011", "legacy": True, "customer_id": "acme",
    "period": "2026-11", "invoice_date": "2026-11-28", "due_date": "2026-12-13",
    "line_items": [{"id": "mgmt-fee", "description": "Management fee", "detail": "",
                    "qty": "1", "unit_price": "8400.00"}],
}


class ToolsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.handoff = os.path.join(self.tmp, "handoff")
        self.outbox = os.path.join(self.tmp, "outbox")
        os.mkdir(self.handoff)
        os.mkdir(self.outbox)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data,
                                               "CASA_HANDOFF_DIR": self.handoff,
                                               "CASA_PLUGIN_OUTBOX_DIR": self.outbox})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._id = 0

    # helpers ------------------------------------------------------------
    def rpc(self, name, args=None):
        self._id += 1
        resp = invoices_server.handle({"jsonrpc": "2.0", "id": self._id,
                                       "method": "tools/call",
                                       "params": {"name": name, "arguments": args or {}}})
        result = resp["result"]
        self.assertEqual(len(result["content"]), 1)
        return result["content"][0]["text"], bool(result.get("isError"))

    def ok(self, name, args=None):
        text, err = self.rpc(name, args)
        self.assertFalse(err, f"{name}: {text}")
        return text

    def ok_json(self, name, args=None):
        return json.loads(self.ok(name, args))

    def fails(self, name, args=None):
        text, err = self.rpc(name, args)
        self.assertTrue(err, f"{name} should fail: {text}")
        self.assertTrue(text.startswith("error: "), text)
        return text

    def setup_company(self, **extra):
        return self.ok_json("invoice_company_set", {"fields": COMPANY, **extra})

    def setup_customer(self, cid="acme", **fields):
        return self.ok_json("invoice_customer_set",
                            {"customer": cid, "fields": {**CUSTOMER, **fields}})

    @staticmethod
    def draft_and_ref(summary):
        draft = re.search(r"^Draft: (\S+)$", summary, re.M).group(1)
        ref = re.search(r"^Ref: (\S+)$", summary, re.M).group(1)
        return draft, ref

    def new(self, **args):
        return self.draft_and_ref(self.ok("invoice_new", {"customer": "acme", **args}))

    def finalize(self, draft, ref):
        return self.ok_json("invoice_finalize",
                            {"draft_id": draft, "customer": "Acme Biotech B.V.", "approval_ref": ref})


class TestFlow(ToolsTestBase):
    def test_setup_then_like_last_time_flow(self):
        self.setup_company()
        self.setup_customer()
        summary = self.ok("invoice_new", {"customer": "acme", "from": "none",
                                          "items": ITEMS, "period": "2026-10",
                                          "invoice_date": "2026-10-31"})
        self.assertNotIn("Copied from", summary)
        draft, ref = self.draft_and_ref(summary)
        pkg = self.finalize(draft, ref)
        self.assertEqual(pkg["invoice_number"], "INV-2026-0001")
        self.assertTrue(pkg["pdf_path"].startswith(self.handoff + os.sep))
        self.assertEqual(pkg["total"], "10596.99")
        self.assertEqual(pkg["email"]["to"], "ap@example.com")

        # "like last time": no from -> the previous invoice, lines copied, period advanced
        summary2 = self.ok("invoice_new", {"customer": "acme", "invoice_date": "2026-11-30"})
        self.assertIn("Copied from: INV-2026-0001", summary2)
        self.assertIn("November 2026", summary2)
        draft2, ref2 = self.draft_and_ref(summary2)
        got = self.ok_json("invoice_get", {"id": "INV-2026-0001"})
        self.assertEqual(got["invoice_number"], "INV-2026-0001")
        self.assertEqual(self.finalize(draft2, ref2)["invoice_number"], "INV-2026-0002")

    def test_finalize_ref_mismatch_is_tool_error_with_new_summary(self):
        self.setup_company()
        self.setup_customer()
        draft, ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        self.ok("invoice_edit", {"draft_id": draft, "operations": [
            {"op": "remove_item", "id": "L2"}]})
        text = self.fails("invoice_finalize", {"draft_id": draft, "customer": "ACME",
                                               "approval_ref": ref})
        self.assertIn("approval", text)
        new_ref = re.search(r"^Ref: (\S+)$", text, re.M).group(1)
        self.assertNotEqual(new_ref, ref)
        self.assertEqual(self.finalize(draft, new_ref)["total"], "10164.00")

    def test_template_change_after_summary_voids_approval(self):
        self.setup_company()
        self.setup_customer()
        draft, ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        self.ok("invoice_customer_set", {"customer": "acme", "fields": {
            "email_template": "Subject: {invoice_number}\n\nPay to another account.\n"}})
        text = self.fails("invoice_finalize", {"draft_id": draft, "customer": "acme",
                                               "approval_ref": ref})
        self.assertIn("Nothing was issued", text)
        self.assertEqual(store.listdir("invoices"), [])
        self.assertEqual(store.listdir("finalizing"), [])
        self.assertTrue(store.exists(f"drafts/{draft}.json"))
        new_ref = re.search(r"^Ref: (\S+)$", text, re.M).group(1)
        pkg = self.finalize(draft, new_ref)
        self.assertEqual(pkg["email"]["body"], "Pay to another account.\n")

    def test_finalize_refuses_another_customer(self):
        self.setup_company()
        self.setup_customer()
        self.setup_customer("globex", name="Globex")
        draft, ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        for wrong in ("Globex", "globex", "Acme Biotech", ""):
            text = self.fails("invoice_finalize", {"draft_id": draft, "customer": wrong,
                                                   "approval_ref": ref})
            self.assertIn("Acme Biotech B.V.", text)
            self.assertEqual(store.listdir("invoices"), [])
            self.assertEqual(store.listdir("finalizing"), [])
        pkg = self.ok_json("invoice_finalize", {"draft_id": draft,
                                                "customer": "  acme biotech b.v. ",
                                                "approval_ref": ref})
        # A retry of the issued draft is checked against the issued invoice.
        self.fails("invoice_finalize", {"draft_id": draft, "customer": "Globex",
                                        "approval_ref": ref})
        again = self.ok_json("invoice_finalize", {"draft_id": draft, "customer": "ACME",
                                                  "approval_ref": ref})
        self.assertEqual(again["invoice_number"], pkg["invoice_number"])

    def test_finalize_is_idempotent(self):
        self.setup_company()
        self.setup_customer()
        draft, ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        first = self.finalize(draft, ref)
        again = self.finalize(draft, ref)
        self.assertEqual(again["invoice_number"], first["invoice_number"])


class TestErrors(ToolsTestBase):
    def test_error_is_tool_error(self):
        self.setup_company()
        self.setup_customer()
        text = self.fails("invoice_new", {"customer": "acme", "items": [
            {"description": "Fee", "qty": "1", "unit_price": "13.000,00"}]})
        self.assertIn("13000.00", text)            # names the accepted format
        self.assertEqual(store.listdir("drafts"), [])

    def test_unexpected_exception_names_its_type(self):
        self.setup_company()
        with mock.patch.object(store, "listdir", side_effect=RuntimeError("boom")):
            text = self.fails("invoice_list")
        self.assertEqual(text, "error: RuntimeError: boom")

    def test_tools_refuse_before_setup(self):
        for name, args in (("invoice_new", {"customer": "acme", "items": ITEMS}),
                           ("invoice_list", {}), ("invoice_get", {"id": "X"}),
                           ("invoice_preview", {"draft_id": "X"}),
                           ("invoice_package", {"invoice_number": "INV-2026-0001"}),
                           ("invoice_discard", {"draft_id": "X"}),
                           ("invoice_edit", {"draft_id": "X", "operations": []}),
                           ("invoice_finalize", {"draft_id": "X", "customer": "c",
                                                 "approval_ref": "r"})):
            self.assertIn("Set up the company first (invoice_company_set)",
                          self.fails(name, args), name)
        # company/customer tools work before setup
        self.ok("invoice_company_get")
        self.ok("invoice_customer_list")
        self.setup_customer()
        self.ok("invoice_customer_get", {"customer": "acme"})

    def test_unknown_tool_and_method(self):
        resp = invoices_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32601)
        resp = invoices_server.handle({"jsonrpc": "2.0", "id": 2, "method": "bogus"})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertIsNone(invoices_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_server_survives_odd_input(self):
        lines = [
            '{"jsonrpc": "2.0", "id": ' + "9" * 5000 + ', "method": "tools/list"}',
            '{"jsonrpc": "2.0", "id": 5, "x": ' + "[" * 1100 + "]" * 1100 + "}",
            "not json",
            "[1, 2]",
            '{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": [1]}',
            '{"jsonrpc": "2.0", "method": "tools/call", "params": [1]}',
            '{"jsonrpc": "2.0", "method": "bogus/notification"}',
            '{"jsonrpc": "2.0", "id": 8, "method": "tools/list"}',
        ]
        proc = subprocess.run([sys.executable, str(SERVER / "invoices_server.py")],
                              input="\n".join(lines) + "\n", capture_output=True,
                              text=True, timeout=30,
                              env={**os.environ, "CLAUDE_PLUGIN_DATA": self.data})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in out], [7, 8], out)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertEqual(len(out[1]["result"]["tools"]), 14)

    def test_read_tools_skip_a_record_deleted_while_listing(self):
        self.setup_company()
        self.setup_customer()
        draft, _ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        real = store.read_json

        def gone(rel):
            return None if rel == f"drafts/{draft}.json" else real(rel)
        with mock.patch.object(store, "read_json", side_effect=gone):
            self.assertEqual(self.ok_json("invoice_list")["drafts"], [])
            self.ok_json("invoice_customer_list")

    def test_tools_list(self):
        resp = invoices_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        listed = resp["result"]["tools"]
        self.assertEqual(len(listed), 14)
        for t in listed:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertTrue(t["description"], t["name"])

    def test_writes_take_the_lock_reads_do_not(self):
        self.setup_company()
        self.setup_customer()
        self.addCleanup(setattr, store, "LOCK_WAIT_S", store.LOCK_WAIT_S)
        store.LOCK_WAIT_S = 0.1
        with store.write_lock():
            self.assertIn("busy", self.fails("invoice_customer_set",
                                             {"customer": "x", "fields": CUSTOMER}))
            self.ok("invoice_customer_list")
        self.assertFalse(store.exists("customers/x.json"))


class TestCompany(ToolsTestBase):
    def test_last_invoice_number_sets_floor(self):
        out = self.setup_company(last_invoice_number="INV-2026-0041")
        self.assertEqual(out["company"]["numbering"]["next_seq"], 42)
        self.assertEqual(out["company"]["numbering"]["seq_year"], 2026)
        self.setup_customer()
        draft, ref = self.new(items=ITEMS, invoice_date="2026-11-30")
        self.assertEqual(self.finalize(draft, ref)["invoice_number"], "INV-2026-0042")

    def test_last_invoice_number_must_parse(self):
        text = self.fails("invoice_company_set", {"fields": COMPANY,
                                                  "last_invoice_number": "2026/41"})
        self.assertIn("2026/41", text)
        self.assertFalse(store.exists("company.json"))

    def test_bad_pattern_refused(self):
        text = self.fails("invoice_company_set",
                          {"fields": {**COMPANY, "numbering": {"pattern": "INV-{seq:4}"}}})
        self.assertIn("numbering.pattern", text)

    def test_pattern_with_slash_refused_and_nothing_written(self):
        text = self.fails("invoice_company_set", {"fields": {
            **COMPANY, "numbering": {"pattern": "{YYYY}/{seq:3}"}}})
        self.assertIn("numbering.pattern", text)
        self.assertFalse(store.exists("company.json"))
        self.setup_company()
        before = store.read_json("company.json")
        self.fails("invoice_company_set",
                   {"fields": {"numbering": {"pattern": "{YYYY}/{seq:3}"}}})
        self.assertEqual(store.read_json("company.json"), before)

    def test_pattern_change_carries_series_forward(self):
        self.setup_company()
        self.setup_customer()
        year = int(tools.model.today("Europe/Amsterdam")[:4])
        for _ in range(2):
            draft, ref = self.new(items=ITEMS, invoice_date=f"{year}-01-15")
            self.finalize(draft, ref)
        out = self.ok_json("invoice_company_set",
                           {"fields": {"numbering": {"pattern": "F{YYYY}.{seq:3}"}}})
        self.assertEqual(out["company"]["numbering"]["next_seq"], 3)
        self.assertEqual(out["company"]["numbering"]["seq_year"], year)
        self.assertEqual(out["next_invoice_number"], f"F{year}.003")
        draft, ref = self.new(items=ITEMS, invoice_date=f"{year}-02-15")
        self.assertEqual(self.finalize(draft, ref)["invoice_number"], f"F{year}.003")

    def test_pattern_change_refused_while_a_floor_for_another_year_is_set(self):
        year = int(tools.model.today("Europe/Amsterdam")[:4])
        self.setup_company(last_invoice_number=f"INV-{year - 1}-0041")
        self.setup_customer()
        before = store.read_json("company.json")
        text = self.fails("invoice_company_set",
                          {"fields": {"numbering": {"pattern": "F{YYYY}.{seq:3}"}}})
        self.assertIn(f"starting number for {year - 1}", text)
        self.assertEqual(store.read_json("company.json"), before)
        # The floor still holds: a draft dated in that year continues after 0041.
        draft, ref = self.new(items=ITEMS, invoice_date=f"{year - 1}-12-15")
        self.assertEqual(self.finalize(draft, ref)["invoice_number"], f"INV-{year - 1}-0042")
        # A floor for the current year lifts the refusal.
        self.ok("invoice_company_set", {"fields": {}, "last_invoice_number": f"INV-{year}-0003"})
        out = self.ok_json("invoice_company_set",
                           {"fields": {"numbering": {"pattern": "F{YYYY}.{seq:3}"}}})
        self.assertEqual(out["next_invoice_number"], f"F{year}.004")

    def test_next_seq_without_seq_year_applies_to_this_year(self):
        year = int(tools.model.today("Europe/Amsterdam")[:4])
        out = self.ok_json("invoice_company_set",
                           {"fields": {**COMPANY, "numbering": {"next_seq": 42}}})
        self.assertEqual(out["company"]["numbering"]["seq_year"], year)
        self.assertEqual(out["next_invoice_number"], f"INV-{year}-0042")

    def test_pattern_change_with_explicit_next_seq(self):
        self.setup_company()
        out = self.ok_json("invoice_company_set", {"fields": {"numbering": {
            "pattern": "F{YYYY}.{seq:3}", "next_seq": 7}}})
        self.assertEqual(out["company"]["numbering"]["next_seq"], 7)

    def test_company_get_and_partial_update(self):
        self.assertIn("invoice_company_set", self.ok("invoice_company_get"))
        self.setup_company()
        self.ok("invoice_company_set", {"fields": {"bank": "ING"}})
        got = self.ok_json("invoice_company_get")
        self.assertEqual(got["bank"], "ING")
        self.assertEqual(got["name"], COMPANY["name"])


class TestCustomers(ToolsTestBase):
    def test_customer_crud(self):
        self.setup_customer()
        self.setup_customer("globex", name="Globex")
        listed = self.ok_json("invoice_customer_list")["customers"]
        self.assertEqual([c["id"] for c in listed], ["acme", "globex"])
        self.ok("invoice_customer_set", {"customer": "globex",
                                         "fields": {"email": {"cc": ["x@example.com"]}}})
        got = self.ok_json("invoice_customer_get", {"customer": "globex"})
        self.assertEqual(got["email"]["to"], ["ap@example.com"])
        self.assertEqual(got["email"]["cc"], ["x@example.com"])
        self.ok("invoice_customer_remove", {"customer": "globex"})
        self.fails("invoice_customer_get", {"customer": "globex"})
        self.fails("invoice_customer_remove", {"customer": "globex"})

    def test_customer_id_cannot_differ(self):
        self.fails("invoice_customer_set", {"customer": "acme",
                                            "fields": {**CUSTOMER, "id": "other"}})

    def test_bad_email_template_refused(self):
        text = self.fails("invoice_customer_set", {"customer": "acme", "fields": {
            **CUSTOMER, "email_template": "Subject: {nope}\n\nHi"}})
        self.assertIn("nope", text)

    def test_remove_refused_with_open_drafts(self):
        self.setup_company()
        self.setup_customer()
        self.new(items=ITEMS)
        text = self.fails("invoice_customer_remove", {"customer": "acme"})
        self.assertIn("draft", text)
        self.assertTrue(store.exists("customers/acme.json"))


class TestDrafts(ToolsTestBase):
    def setUp(self):
        super().setUp()
        self.setup_company()
        self.setup_customer()

    def test_new_without_prior_invoice_needs_items(self):
        self.assertIn("items", self.fails("invoice_new", {"customer": "acme"}))
        self.fails("invoice_new", {"customer": "acme", "from": "last"})

    def test_new_from_explicit_number_of_other_customer_refused(self):
        self.setup_customer("globex", name="Globex")
        draft, ref = self.new(items=ITEMS, invoice_date="2026-10-31")
        number = self.finalize(draft, ref)["invoice_number"]
        self.fails("invoice_new", {"customer": "globex", "from": number})
        self.fails("invoice_new", {"customer": "acme", "from": "INV-2099-0001"})
        summary = self.ok("invoice_new", {"customer": "acme", "from": number})
        self.assertIn(f"Copied from: {number}", summary)

    def test_last_picks_latest_invoice_date_including_legacy(self):
        store.write_json(f"invoices/{LEGACY['invoice_number']}.json", LEGACY)
        draft, ref = self.new(items=ITEMS, invoice_date="2026-10-31", period="2026-10")
        self.finalize(draft, ref)
        summary = self.ok("invoice_new", {"customer": "acme"})
        self.assertIn("Copied from: INV-2026-0011", summary)   # legacy, newer date
        self.assertIn("December 2026", summary)

    def test_new_with_unknown_customer(self):
        self.fails("invoice_new", {"customer": "ghost", "items": ITEMS})

    def test_edit_and_get_draft(self):
        draft, _ = self.new(items=ITEMS)
        summary = self.ok("invoice_edit", {"draft_id": draft, "operations": [
            {"op": "change_item", "id": "L1", "field": "unit_price", "value": "13000.00"},
            {"op": "add_item", "item": {"description": "Extra", "qty": "1",
                                        "unit_price": "10.00"}},
            {"op": "set", "field": "notes", "value": "Thanks"}]})
        self.assertIn("Notes: Thanks", summary)
        self.assertEqual(self.ok("invoice_get", {"id": draft}), summary)
        bad = self.fails("invoice_edit", {"draft_id": draft, "operations": [
            {"op": "remove_item", "id": "nope"}]})
        self.assertIn("nope", bad)

    def test_edit_and_discard_refuse_locked_draft(self):
        draft, _ = self.new(items=ITEMS)
        with mock.patch.object(finalize, "is_draft_locked", return_value=True):
            self.fails("invoice_edit", {"draft_id": draft, "operations": [
                {"op": "set", "field": "notes", "value": "x"}]})
            self.fails("invoice_discard", {"draft_id": draft})
        self.assertTrue(store.exists(f"drafts/{draft}.json"))

    def test_discard(self):
        draft, _ = self.new(items=ITEMS)
        self.ok("invoice_discard", {"draft_id": draft})
        self.assertFalse(store.exists(f"drafts/{draft}.json"))
        self.fails("invoice_discard", {"draft_id": draft})

    def test_preview_writes_pdf_to_outbox(self):
        draft, _ = self.new(items=ITEMS)
        text = self.ok("invoice_preview", {"draft_id": draft})
        path = text.removeprefix("Path: ")
        self.assertTrue(text.startswith("Path: "))
        self.assertEqual(os.path.dirname(path), self.outbox)
        self.assertRegex(os.path.basename(path), rf"^invoice-draft-{draft}-[0-9a-f]{{6}}\.pdf$")
        with open(path, "rb") as f:
            self.assertTrue(f.read().startswith(b"%PDF"))
        self.assertEqual([n for n in os.listdir(self.outbox) if n.endswith(".part")], [])

    def test_list_and_get_with_legacy(self):
        store.write_json(f"invoices/{LEGACY['invoice_number']}.json", LEGACY)
        draft, ref = self.new(items=ITEMS, invoice_date="2026-12-31", period="2026-12")
        issued = self.finalize(draft, ref)["invoice_number"]
        open_draft, _ = self.new(items=ITEMS, invoice_date="2027-01-05")
        out = self.ok_json("invoice_list")
        self.assertEqual([d["draft_id"] for d in out["drafts"]], [open_draft])
        self.assertEqual([i["invoice_number"] for i in out["invoices"]],
                         [issued, "INV-2026-0011"])
        self.assertTrue(out["invoices"][1]["legacy"])
        self.assertEqual(out["invoices"][0]["total"], "€ 10.596,99")
        self.assertEqual(self.ok_json("invoice_list", {"year": 2027})["invoices"], [])
        self.assertEqual(self.ok_json("invoice_list", {"customer": "globex"})["drafts"], [])
        legacy = self.ok_json("invoice_get", {"id": "INV-2026-0011"})
        self.assertTrue(legacy["legacy"])
        self.assertEqual(legacy["line_items"][0]["unit_price"], "8400.00")
        full = self.ok_json("invoice_get", {"id": issued})
        self.assertEqual(full["totals"]["total"], "€ 10.596,99")
        self.fails("invoice_get", {"id": "NOPE"})

    def test_package_reissues_and_refuses_legacy(self):
        store.write_json(f"invoices/{LEGACY['invoice_number']}.json", LEGACY)
        draft, ref = self.new(items=ITEMS, invoice_date="2026-12-31")
        first = self.finalize(draft, ref)
        again = self.ok_json("invoice_package", {"invoice_number": first["invoice_number"]})
        self.assertEqual(again["email"], first["email"])
        self.assertNotEqual(again["pdf_path"], first["pdf_path"])
        self.assertIn("Gmail", self.fails("invoice_package",
                                          {"invoice_number": "INV-2026-0011"}))


class TestLikeLastTimeAfterSetup(ToolsTestBase):
    def test_floor_then_like_last_time_continues_the_series(self):
        # an existing series taken over at setup, then "like last time" from the first
        # invoice issued here: lines copied, period advanced, numbering continues
        out = self.setup_company(last_invoice_number="INV-2026-0011")
        self.assertEqual(out["company"]["numbering"]["next_seq"], 12)
        self.setup_customer()
        draft, ref = self.new(**{"from": "none"}, items=ITEMS, period="2026-11",
                              invoice_date="2026-11-28")
        self.assertEqual(self.finalize(draft, ref)["invoice_number"], "INV-2026-0012")

        summary = self.ok("invoice_new", {"customer": "acme", "invoice_date": "2026-12-03"})
        self.assertIn("Copied from: INV-2026-0012", summary)
        self.assertIn("December 2026", summary)
        draft2, ref2 = self.draft_and_ref(summary)
        pkg = self.finalize(draft2, ref2)
        self.assertEqual(pkg["invoice_number"], "INV-2026-0013")
        self.assertEqual(pkg["total"], "10596.99")
        self.assertEqual(pkg["email"]["to"], "ap@example.com")
        first = self.ok_json("invoice_get", {"id": "INV-2026-0012"})
        second = self.ok_json("invoice_get", {"id": "INV-2026-0013"})
        self.assertEqual(second["period"], "2026-12")
        self.assertEqual(second["line_items"], first["line_items"])
        listed = [r["invoice_number"] for r in self.ok_json("invoice_list")["invoices"]]
        self.assertEqual(sorted(listed), ["INV-2026-0012", "INV-2026-0013"])


if __name__ == "__main__":
    unittest.main()
