# tests/test_legacy.py
"""One-time import of lesina-invoice data (spec 5.7)."""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
import email_template  # noqa: E402
import finalize  # noqa: E402
import invoice  # noqa: E402
import legacy  # noqa: E402
import model  # noqa: E402
import numbering  # noqa: E402
import store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "legacy"
EXTRA = FIXTURES / "extra"


class LegacyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "plugins", "data", "invoices-inline")
        self.handoff = os.path.join(self.tmp, "handoff")
        os.mkdir(self.handoff)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data,
                                               "CASA_HANDOFF_DIR": self.handoff})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.legacy = os.path.join(self.tmp, "plugins", "data", "lesina-invoice-inline")
        shutil.copytree(FIXTURES, self.legacy, ignore=shutil.ignore_patterns("extra"))

    # helpers ------------------------------------------------------------
    def add_draft(self, extra_name: str, period: str) -> None:
        shutil.copy(EXTRA / extra_name, os.path.join(self.legacy, "drafts", f"{period}.json"))

    def write_draft(self, period: str, draft: dict) -> None:
        with open(os.path.join(self.legacy, "drafts", f"{period}.json"), "w") as f:
            json.dump(draft, f)

    def legacy_draft(self, period: str) -> dict:
        with open(os.path.join(self.legacy, "drafts", f"{period}.json")) as f:
            return json.load(f)

    def set_defaults(self, **changes) -> None:
        p = os.path.join(self.legacy, "defaults.json")
        with open(p) as f:
            d = json.load(f)
        d.update(changes)
        with open(p, "w") as f:
            json.dump(d, f)

    def data_files(self) -> list[str]:
        out = []
        for dirpath, _dirs, files in os.walk(self.data):
            out += [os.path.relpath(os.path.join(dirpath, f), self.data) for f in files]
        return sorted(f for f in out if f != "state.lock")

    def run_import(self, force=False):
        with store.write_lock():
            return legacy.import_legacy(self.legacy, force)


class TestImport(LegacyTestBase):
    def test_imports_company_customer_and_invoices(self):
        res = self.run_import()
        self.assertEqual(res["company"], "imported")
        self.assertEqual(res["customer"], "acme")
        self.assertEqual(res["invoices"], ["INV-2026-0009", "INV-2026-0010", "INV-2026-0011"])
        self.assertEqual(res["ignored_drafts"], [])
        self.assertIn("n8n", res["runbook"])
        self.assertIn("Uninstall lesina-invoice", res["runbook"])

        company = store.read_json("company.json")
        self.assertEqual(company["name"], "Example Holding B.V.")
        self.assertEqual(company["address_lines"], ["Voorbeeldstraat 1", "1234 AB Amsterdam"])
        self.assertEqual(company["country"], "The Netherlands")
        self.assertEqual(company["vat_id"], "NL000099998B57")
        self.assertEqual(company["registration_id"], "12345678")
        self.assertEqual(company["email"], "billing@example.com")
        self.assertEqual(company["iban"], "NL91ABNA0417164300")
        self.assertEqual(company["bank"], "Example Bank")
        self.assertEqual(company["vat_label"], "BTW-id")
        self.assertEqual(company["registration_label"], "KvK")
        self.assertEqual(company["tax_name"], "BTW")
        self.assertEqual(company["default_vat_rate"], "21")
        self.assertEqual(company["payment_terms_days"], 15)
        self.assertEqual(company["locale"], "nl-NL")
        self.assertEqual(company["timezone"], "Europe/Amsterdam")
        self.assertEqual(company["currency"], "EUR")
        self.assertEqual(model.validate_company(json.loads(json.dumps(company))), company)

        customer = store.read_json("customers/acme.json")
        self.assertEqual(customer["id"], "acme")
        self.assertEqual(customer["name"], "Acme Biotech B.V.")
        self.assertEqual(customer["address_lines"], ["Testlaan 2", "5678 CD Utrecht"])
        self.assertEqual(customer["vat_id"], "NL000011112B34")
        self.assertEqual(customer["registration_id"], "87654321")
        self.assertEqual(customer["email"], {"to": ["ap@example.com"],
                                             "cc": ["inbox@example.com"], "bcc": []})

        rec = store.read_json("invoices/INV-2026-0010.json")
        self.assertEqual(set(rec), {"invoice_number", "legacy", "customer_id", "period",
                                    "invoice_date", "due_date", "line_items"})
        self.assertEqual(rec["invoice_number"], "INV-2026-0010")
        self.assertIs(rec["legacy"], True)
        self.assertEqual(rec["customer_id"], "acme")
        self.assertEqual(rec["period"], "2026-10")
        self.assertEqual(rec["invoice_date"], "2026-10-15")
        self.assertEqual(rec["due_date"], "2026-10-30")
        self.assertEqual(len(rec["line_items"]), 8)
        self.assertEqual(rec["line_items"][0], {"id": "mgmt-fee", "description": "Management fee",
                                                "detail": "", "qty": "1",
                                                "unit_price": "8400.00"})
        self.assertEqual([i["id"] for i in rec["line_items"]],
                         ["mgmt-fee", "travel", "tool-a", "tool-b", "tool-c", "card",
                          "tool-d", "tool-e"])
        self.assertEqual(self.data_files(), [
            "company.json", "customers/acme.json", "invoices/INV-2026-0009.json",
            "invoices/INV-2026-0010.json", "invoices/INV-2026-0011.json"])

    def test_customer_id_is_derived_from_the_recipient_name(self):
        self.assertEqual(legacy.customer_id("Acme Biotech B.V."), "acme")
        self.assertEqual(legacy.customer_id("  O'Brien-Smith & Co"), "obriensmith")
        self.assertEqual(legacy.customer_id("!!! B.V."), "customer")
        self.assertEqual(legacy.customer_id(None), "customer")
        self.assertEqual(legacy.customer_id("x" * 60), "x" * 40)
        res = self.run_import()
        self.assertEqual(res["customer"], "acme")
        self.assertEqual(store.read_json("invoices/INV-2026-0009.json")["customer_id"], "acme")

    def test_step_0_names_no_amount(self):
        self.assertNotRegex(legacy.STEP_0, r"[0-9]")

    def test_legacy_record_cannot_be_packaged(self):
        self.run_import()
        with self.assertRaisesRegex(model.ValidationError, "lesina-invoice"):
            finalize.package("INV-2026-0011")

    def test_btw_rate_converts_without_float_artefacts(self):
        for btw, expected in ((0.21, "21"), (0.09, "9"), (0.155, "15.5"), (0, "0"),
                              (0.07, "7"), (0.29, "29")):
            with self.subTest(btw=btw):
                self.set_defaults(btw_rate=btw)
                self.run_import(force=True)
                self.assertEqual(store.read_json("company.json")["default_vat_rate"], expected)

    def test_fractional_qty_kept_exactly(self):
        d = self.legacy_draft("2026-11")
        d["line_items"][1]["qty"] = 1.1
        self.write_draft("2026-11", d)
        self.run_import()
        self.assertEqual(store.read_json("invoices/INV-2026-0011.json")["line_items"][1]["qty"],
                         "1.1")

    def test_boolean_only_draft_counts_as_submitted(self):
        self.add_draft("2026-08-boolean.json", "2026-08")
        res = self.run_import()
        self.assertEqual(res["invoices"], ["INV-2026-0008", "INV-2026-0009",
                                           "INV-2026-0010", "INV-2026-0011"])
        rec = store.read_json("invoices/INV-2026-0008.json")
        self.assertEqual(rec["period"], "2026-08")
        self.assertIs(rec["legacy"], True)

    def test_boolean_only_unsent_draft_is_idle(self):
        self.add_draft("2026-07-boolean-unsent.json", "2026-07")
        res = self.run_import()
        self.assertEqual(res["ignored_drafts"], ["2026-07"])
        self.assertFalse(store.exists("invoices/INV-2026-0007.json"))

    def test_pending_refuses_and_writes_nothing(self):
        self.add_draft("2026-12-pending.json", "2026-12")
        with self.assertRaises(model.ValidationError) as cm:
            self.run_import()
        msg = str(cm.exception)
        self.assertIn("2026-12", msg)
        self.assertIn("pending", msg)
        self.assertEqual(self.data_files(), [])

    def test_pending_wins_over_explicit_submitted_boolean(self):
        d = self.legacy_draft("2026-11")
        d["submit_state"] = "pending"          # submitted stays true
        self.write_draft("2026-11", d)
        with self.assertRaisesRegex(model.ValidationError, "pending"):
            self.run_import()
        self.assertEqual(self.data_files(), [])

    def test_unknown_state_refuses(self):
        d = self.legacy_draft("2026-11")
        d["submit_state"] = "sent"
        self.write_draft("2026-11", d)
        with self.assertRaisesRegex(model.ValidationError, "sent"):
            self.run_import()
        self.assertEqual(self.data_files(), [])

    def test_idle_drafts_ignored_and_reported(self):
        self.add_draft("2026-12-idle.json", "2026-12")
        res = self.run_import()
        self.assertEqual(res["ignored_drafts"], ["2026-12"])
        self.assertEqual(res["invoices"], ["INV-2026-0009", "INV-2026-0010", "INV-2026-0011"])
        self.assertFalse(store.exists("invoices/INV-2026-0012.json"))
        self.assertEqual(res["next_number"], "INV-2026-0012")

    def test_missing_defaults_refuses_with_runbook_step(self):
        os.unlink(os.path.join(self.legacy, "defaults.json"))
        with self.assertRaises(model.ValidationError) as cm:
            self.run_import()
        msg = str(cm.exception)
        self.assertIn("defaults.json", msg)
        self.assertIn("re-save", msg)
        self.assertIn("lesina-invoice", msg)
        self.assertEqual(self.data_files(), [])

    def test_missing_dir_refuses(self):
        shutil.rmtree(self.legacy)
        with self.assertRaisesRegex(model.ValidationError, "lesina-invoice-inline"):
            self.run_import()
        self.assertEqual(self.data_files(), [])

    def test_bad_item_names_the_file_and_writes_nothing(self):
        d = self.legacy_draft("2026-10")
        d["line_items"][0]["unit_price"] = "8.400,00"
        self.write_draft("2026-10", d)
        with self.assertRaisesRegex(model.ValidationError, "2026-10"):
            self.run_import()
        self.assertEqual(self.data_files(), [])

    def test_number_outside_the_period_year_refuses(self):
        d = self.legacy_draft("2026-11")
        d["invoice_number"] = "INV-2025-0011"
        self.write_draft("2026-11", d)
        with self.assertRaisesRegex(model.ValidationError, "INV-2025-0011"):
            self.run_import()
        self.assertEqual(self.data_files(), [])

    def test_default_legacy_dir(self):
        self.assertEqual(legacy.default_legacy_dir(), self.legacy)
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data + "/"}):
            self.assertEqual(legacy.default_legacy_dir(), self.legacy)
        with store.write_lock():
            res = legacy.import_legacy(None, False)
        self.assertEqual(res["invoices"], ["INV-2026-0009", "INV-2026-0010", "INV-2026-0011"])


class TestCounter(LegacyTestBase):
    def test_counter_continues(self):
        res = self.run_import()
        self.assertEqual(res["next_number"], "INV-2026-0012")
        company = store.read_json("company.json")
        self.assertEqual(company["numbering"], {"pattern": "INV-{YYYY}-{seq:4}",
                                                "next_seq": 12, "seq_year": 2026})
        stems = [n[:-5] for n in store.listdir("invoices")]
        self.assertEqual(numbering.next_number(company["numbering"]["pattern"], 2026, stems,
                                               company["numbering"]),
                         ("INV-2026-0012", 12))

    def test_counter_takes_the_latest_year(self):
        self.write_draft("2025-12", {**self.legacy_draft("2026-11"),
                                     "invoice_number": "INV-2025-0012",
                                     "invoice_date": "2025-12-04", "due_date": "2025-12-19",
                                     "service_period": "December 2025"})
        res = self.run_import()
        self.assertEqual(res["invoices"][0], "INV-2025-0012")
        self.assertEqual(store.read_json("company.json")["numbering"],
                         {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 12, "seq_year": 2026})
        self.assertEqual(res["next_number"], "INV-2026-0012")

    def test_forced_import_never_lowers_a_floor_raised_by_hand(self):
        self.run_import()
        company = store.read_json("company.json")
        # As invoice_company_set with last_invoice_number INV-2026-0020 leaves it.
        company["numbering"] = {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 21,
                                "seq_year": 2026}
        store.write_json("company.json", company)
        res = self.run_import(force=True)
        self.assertEqual(res["next_number"], "INV-2026-0021")
        self.assertEqual(store.read_json("company.json")["numbering"],
                         {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 21, "seq_year": 2026})

    def test_forced_import_ignores_a_floor_of_another_year(self):
        self.run_import()
        company = store.read_json("company.json")
        company["numbering"] = {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 50,
                                "seq_year": 2025}
        store.write_json("company.json", company)
        self.assertEqual(self.run_import(force=True)["next_number"], "INV-2026-0012")

    def test_counter_ignores_a_gap_below_the_max(self):
        os.unlink(os.path.join(self.legacy, "drafts", "2026-10.json"))
        res = self.run_import()
        self.assertEqual(res["next_number"], "INV-2026-0012")


class TestRerun(LegacyTestBase):
    def test_refuses_second_run_without_force(self):
        self.run_import()
        before = {f: pathlib.Path(self.data, f).read_bytes() for f in self.data_files()}
        with self.assertRaisesRegex(model.ValidationError, "force"):
            self.run_import()
        after = {f: pathlib.Path(self.data, f).read_bytes() for f in self.data_files()}
        self.assertEqual(before, after)
        res = self.run_import(force=True)
        self.assertEqual(res["next_number"], "INV-2026-0012")

    def test_refuses_when_only_an_invoice_exists(self):
        store.write_json("invoices/INV-2026-0001.json", {"invoice_number": "INV-2026-0001"})
        with self.assertRaisesRegex(model.ValidationError, "force"):
            self.run_import()
        self.assertFalse(store.exists("company.json"))

    def test_refuses_when_only_the_company_exists(self):
        store.write_json("company.json", {"name": "x"})
        with self.assertRaisesRegex(model.ValidationError, "force"):
            self.run_import()
        self.assertEqual(store.read_json("company.json"), {"name": "x"})

    def test_force_never_replaces_an_invoice_issued_here(self):
        issued = {"invoice_number": "INV-2026-0011", "source_draft": "ABCD", "content": {}}
        store.write_json("invoices/INV-2026-0011.json", issued)
        with self.assertRaisesRegex(model.ValidationError, "INV-2026-0011"):
            self.run_import(force=True)
        self.assertEqual(store.read_json("invoices/INV-2026-0011.json"), issued)
        self.assertFalse(store.exists("company.json"))
        self.assertFalse(store.exists("invoices/INV-2026-0009.json"))


class TestLikeLastTime(LegacyTestBase):
    def test_like_last_time_after_import(self):
        self.run_import()
        company = store.read_json("company.json")
        customer = store.read_json("customers/acme.json")
        last = store.read_json("invoices/INV-2026-0011.json")

        draft = invoice.new_draft(customer, company, last, period=None,
                                  invoice_date="2026-12-03", items=None,
                                  existing_ids=frozenset(finalize.used_draft_ids()))
        self.assertEqual(draft["period"], "2026-12")
        self.assertEqual(len(draft["line_items"]), 7)
        self.assertEqual(draft["line_items"], last["line_items"])
        self.assertEqual(draft["source_invoice"], "INV-2026-0011")
        self.assertEqual(draft["vat_rate"], "21")
        self.assertEqual(draft["due_date"], "2026-12-18")
        store.write_json(f"drafts/{draft['draft_id']}.json", draft)

        ref = invoice.compute_ref(invoice.frozen_content(
            draft, company, customer, email_template.load(customer, str(ROOT))))
        with store.write_lock():
            pkg = finalize.finalize(draft["draft_id"], ref, str(ROOT))
        self.assertEqual(pkg["invoice_number"], "INV-2026-0012")
        rec = store.read_json("invoices/INV-2026-0012.json")
        self.assertEqual(rec["content"]["period"], "2026-12")
        self.assertEqual(pkg["email"]["to"], "ap@example.com")
        self.assertEqual(pkg["email"]["cc"], "inbox@example.com")
        self.assertEqual(pkg["total"], "11180.21")


if __name__ == "__main__":
    unittest.main()
