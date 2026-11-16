# tests/test_finalize.py
"""Finalize under the claim, resume after a crash, idempotency, and the package."""
import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
import email_template  # noqa: E402
import finalize  # noqa: E402
import invoice  # noqa: E402
import invoice_pdf  # noqa: E402
import model  # noqa: E402
import store  # noqa: E402

PLUGIN_ROOT = str(ROOT)

COMPANY = model.validate_company({
    **copy.deepcopy(model.COMPANY_DEFAULTS),
    "name": "Example Holding B.V.",
    "address_lines": ["Keizersgracht 1", "1015 CJ Amsterdam"],
    "country": "Netherlands",
    "vat_id": "NL123456789B01",
    "email": "invoices@example.com",
    "iban": "NL00BANK0417164300",
    "bank": "ABN AMRO",
    "tax_name": "BTW",
})
CUSTOMER = model.validate_customer({
    "id": "acme",
    "name": "Acme Biotech B.V.",
    "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
    "country": "Netherlands",
    "email": {"to": ["ap@example.com", "ap2@example.com"],
              "cc": ["books@example.com"]},
})
ITEMS = [
    {"description": "Management fee", "detail": "Management services November 2026",
     "qty": "1", "unit_price": "8400.00"},
    {"description": "Travel expenses", "qty": "2", "unit_price": "178.92"},
]
STEPS = ("finalizing", "pdf", "invoice", "draft_deleted", "done")

#: Child: say "ready", wait for the go file, then finalize under the write lock
#: exactly as the dispatcher will, and print the number it got.
CHILD = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    import finalize, store
    print("ready", flush=True)
    while not os.path.exists(sys.argv[2]):
        time.sleep(0.005)
    with store.write_lock():
        pkg = finalize.finalize(sys.argv[3], sys.argv[4], sys.argv[5])
    print(pkg["invoice_number"], flush=True)
""")


class Crash(Exception):
    pass


class FinalizeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.handoff = os.path.join(self.tmp, "handoff")
        os.mkdir(self.handoff)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data,
                                               "CASA_HANDOFF_DIR": self.handoff})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, finalize, "_STEP_HOOK", None)
        store.write_json("company.json", COMPANY)
        store.write_json("customers/acme.json", CUSTOMER)

    # helpers ------------------------------------------------------------
    def new_draft(self, items=ITEMS, **kw):
        args = {"period": "2026-11", "invoice_date": "2026-11-30"}
        args.update(kw)
        d = invoice.new_draft(store.read_json("customers/acme.json"),
                              store.read_json("company.json"), None,
                              items=copy.deepcopy(items),
                              existing_ids=frozenset(n[:-5] for n in store.listdir("drafts")),
                              **args)
        store.write_json(f"drafts/{d['draft_id']}.json", d)
        return d["draft_id"], self.ref(d["draft_id"])

    def ref(self, draft_id):
        d = store.read_json(f"drafts/{draft_id}.json")
        customer = store.read_json(f"customers/{d['customer_id']}.json")
        content = invoice.frozen_content(d, store.read_json("company.json"), customer,
                                         email_template.load(customer, PLUGIN_ROOT))
        return invoice.compute_ref(content)

    def files(self, sub):
        return [n for n in store.listdir(sub) if not n.startswith(".")]

    def crash_at(self, step):
        def hook(name):
            if name == step:
                raise Crash(step)
        finalize._STEP_HOOK = hook

    def assert_complete(self, number, draft_id):
        rec = store.read_json(f"invoices/{number}.json")
        self.assertIsNotNone(rec, number)
        self.assertEqual(rec["invoice_number"], number)
        self.assertEqual(rec["source_draft"], draft_id)
        with open(store.path(f"invoices/{number}.pdf"), "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(rec["pdf_sha256"], hashlib.sha256(data).hexdigest())
        # The stored PDF is exactly the frozen content's rendering.
        self.assertEqual(data, invoice_pdf.render_invoice(rec["content"], number, draft=False))
        self.assertFalse(store.exists(f"drafts/{draft_id}.json"))
        return rec


class TestFinalize(FinalizeTestBase):
    def test_finalize_happy_path(self):
        draft_id, ref = self.new_draft()
        draft = store.read_json(f"drafts/{draft_id}.json")
        expected = invoice.frozen_content(draft, COMPANY, CUSTOMER,
                                          email_template.load(CUSTOMER, PLUGIN_ROOT))
        pkg = finalize.finalize(draft_id, ref, PLUGIN_ROOT)

        n = "INV-2026-0001"
        self.assertEqual(self.files("invoices"), [f"{n}.json", f"{n}.pdf"])
        self.assertEqual(self.files("finalizing"), [])
        self.assertEqual(self.files("drafts"), [])
        rec = self.assert_complete(n, draft_id)
        self.assertEqual(rec["content"], expected)
        self.assertEqual(invoice.compute_ref(rec["content"]), ref)
        self.assertTrue(rec["finalized_at"])

        self.assertEqual(set(pkg), {"invoice_number", "pdf_path", "email", "request_id", "total"})
        self.assertEqual(pkg["invoice_number"], n)
        self.assertEqual(pkg["request_id"], f"invoice-{n}")
        self.assertEqual(pkg["total"], "10596.99")   # 8757.84 + 21% = 1839.15
        self.assertTrue(pkg["pdf_path"].startswith(os.path.join(self.handoff, "invoices") + os.sep))
        self.assertTrue(pkg["pdf_path"].endswith(f"/{n}.pdf"))
        with open(pkg["pdf_path"], "rb") as f, open(store.path(f"invoices/{n}.pdf"), "rb") as g:
            self.assertEqual(f.read(), g.read())
        email = pkg["email"]
        self.assertEqual(email, rec["email"])
        self.assertEqual(email["to"], "ap@example.com, ap2@example.com")
        self.assertEqual(email["cc"], "books@example.com")
        self.assertEqual(email["bcc"], "")
        self.assertEqual(email["from_address"], "invoices@example.com")
        self.assertEqual(email["subject"],
                         f"Invoice {n} — Example Holding B.V. — November 2026")
        self.assertIn("Dear Acme Biotech B.V.,", email["body"])
        self.assertIn("€ 10.596,99", email["body"])
        self.assertIn("30-12-2026", email["body"])

    def test_ref_mismatch_refuses_and_writes_nothing(self):
        draft_id, ref = self.new_draft()
        before = store.read_json(f"drafts/{draft_id}.json")
        with self.assertRaises(finalize.RefMismatch) as cm:
            finalize.finalize(draft_id, "0" * 32, PLUGIN_ROOT)
        self.assertIn("summary", str(cm.exception))
        self.assertIn("approv", str(cm.exception))
        # Content approved, then the customer's address changed: the Ref
        # covers the snapshot, so the old approval no longer holds.
        cust = store.read_json("customers/acme.json")
        cust["address_lines"] = ["Elsewhere 1"]
        store.write_json("customers/acme.json", cust)
        with self.assertRaises(finalize.RefMismatch):
            finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        self.assertEqual(self.files("invoices"), [])
        self.assertEqual(self.files("finalizing"), [])
        self.assertEqual(store.read_json(f"drafts/{draft_id}.json"), before)
        self.assertEqual(os.listdir(self.handoff), [])

    def test_missing_draft_refused(self):
        with self.assertRaises(Exception) as cm:
            finalize.finalize("ZZZZ", "0" * 32, PLUGIN_ROOT)
        self.assertIn("ZZZZ", str(cm.exception))
        self.assertEqual(self.files("invoices"), [])

    def test_crash_at_each_step_resumes_same_number(self):
        for step in STEPS:
            with self.subTest(step=step):
                self.setUp()
                a, ref_a = self.new_draft()
                self.crash_at(step)
                with self.assertRaises(Crash):
                    finalize.finalize(a, ref_a, PLUGIN_ROOT)
                finalize._STEP_HOOK = None
                b, ref_b = self.new_draft(items=ITEMS[:1])
                pkg = finalize.finalize(b, ref_b, PLUGIN_ROOT)
                self.assertEqual(pkg["invoice_number"], "INV-2026-0002")
                rec_a = self.assert_complete("INV-2026-0001", a)
                self.assertEqual(invoice.compute_ref(rec_a["content"]), ref_a)
                self.assert_complete("INV-2026-0002", b)
                self.assertEqual(self.files("invoices"), [
                    "INV-2026-0001.json", "INV-2026-0001.pdf",
                    "INV-2026-0002.json", "INV-2026-0002.pdf"])
                self.assertEqual(self.files("finalizing"), [])
                self.assertEqual(self.files("drafts"), [])

    def test_crash_then_retry_same_draft(self):
        for step in STEPS:
            with self.subTest(step=step):
                self.setUp()
                a, ref_a = self.new_draft()
                self.crash_at(step)
                with self.assertRaises(Crash):
                    finalize.finalize(a, ref_a, PLUGIN_ROOT)
                finalize._STEP_HOOK = None
                pkg = finalize.finalize(a, ref_a, PLUGIN_ROOT)
                self.assertEqual(pkg["invoice_number"], "INV-2026-0001")
                self.assert_complete("INV-2026-0001", a)
                self.assertEqual(self.files("finalizing"), [])

    def test_crash_during_resume_still_converges(self):
        a, ref_a = self.new_draft()
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        self.crash_at("invoice")
        with self.assertRaises(Crash):
            finalize.resume_pending()
        finalize._STEP_HOOK = None
        self.assertEqual(finalize.resume_pending(), ["INV-2026-0001"])
        self.assert_complete("INV-2026-0001", a)
        self.assertEqual(finalize.resume_pending(), [])

    def test_resume_rerenders_missing_pdf(self):
        a, ref_a = self.new_draft()
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        finalize._STEP_HOOK = None
        self.assertFalse(store.exists("invoices/INV-2026-0001.pdf"))
        # The live data changes before the resume: the frozen content wins.
        cust = store.read_json("customers/acme.json")
        cust["name"] = "Renamed B.V."
        store.write_json("customers/acme.json", cust)
        self.assertEqual(finalize.resume_pending(), ["INV-2026-0001"])
        rec = self.assert_complete("INV-2026-0001", a)
        self.assertEqual(rec["content"]["customer"]["name"], "Acme Biotech B.V.")

    def test_resume_refuses_a_different_invoice_under_the_claimed_number(self):
        a, ref_a = self.new_draft()
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        finalize._STEP_HOOK = None
        store.write_json("invoices/INV-2026-0001.json",
                         {"invoice_number": "INV-2026-0001", "legacy": True,
                          "content": {"line_items": []}})
        with self.assertRaises(store.StoreError):
            finalize.resume_pending()
        self.assertEqual(self.files("finalizing"), ["INV-2026-0001.json"])
        self.assertTrue(store.exists(f"drafts/{a}.json"))

    def test_finalize_same_draft_twice_returns_same_package(self):
        draft_id, ref = self.new_draft()
        first = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        second = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        self.assertEqual(first["invoice_number"], second["invoice_number"])
        self.assertEqual(first["email"], second["email"])
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(self.files("invoices"), ["INV-2026-0001.json", "INV-2026-0001.pdf"])

    def test_reused_draft_id_with_different_content_refused(self):
        a, ref_a = self.new_draft()
        old = finalize.finalize(a, ref_a, PLUGIN_ROOT)
        # A new draft that drew the same id (e.g. Task 9 not excluding it).
        b, _ = self.new_draft(items=ITEMS[:1])
        d = store.read_json(f"drafts/{b}.json")
        store.delete(f"drafts/{b}.json")
        d["draft_id"] = a
        store.write_json(f"drafts/{a}.json", d)
        ref_new = self.ref(a)
        self.assertNotEqual(ref_new, ref_a)
        with self.assertRaises(finalize.RefMismatch) as cm:
            finalize.finalize(a, ref_new, PLUGIN_ROOT)
        self.assertIn("INV-2026-0001", str(cm.exception))
        self.assertIn("new draft", str(cm.exception))
        self.assertEqual(self.files("invoices"), ["INV-2026-0001.json", "INV-2026-0001.pdf"])
        self.assertEqual(store.read_json(f"drafts/{a}.json"), d)
        # The genuine retry with the original Ref still returns the package.
        self.assertEqual(finalize.finalize(a, ref_a, PLUGIN_ROOT)["total"], old["total"])

    def test_resume_never_reuses_a_foreign_pdf(self):
        a, ref_a = self.new_draft()
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        finalize._STEP_HOOK = None
        store.write_bytes("invoices/INV-2026-0001.pdf", b"%PDF-1.4 foreign")
        self.assertEqual(finalize.resume_pending(), ["INV-2026-0001"])
        self.assert_complete("INV-2026-0001", a)   # bytes == render_invoice(content)

    def test_two_processes_same_draft_one_invoice(self):
        draft_id, ref = self.new_draft()
        go = os.path.join(self.tmp, "go")
        env = dict(os.environ)
        children = [subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SERVER), go, draft_id, ref, PLUGIN_ROOT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for _ in range(2)]
        try:
            for c in children:
                self.assertEqual(c.stdout.readline().strip(), "ready")
            pathlib.Path(go).touch()
            outs = [c.communicate(timeout=60) for c in children]
        finally:
            for c in children:
                if c.poll() is None:
                    c.kill()
                    c.wait()
        for c, (out, err) in zip(children, outs):
            self.assertEqual(c.returncode, 0, err)
        numbers = [out.strip() for out, _ in outs]
        self.assertEqual(numbers, ["INV-2026-0001", "INV-2026-0001"])
        self.assertEqual(self.files("invoices"), ["INV-2026-0001.json", "INV-2026-0001.pdf"])
        self.assertEqual(self.files("finalizing"), [])

    def test_edit_refused_while_finalizing(self):
        a, ref_a = self.new_draft()
        b, _ = self.new_draft()
        self.assertFalse(finalize.is_draft_locked(a))
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        self.assertTrue(finalize.is_draft_locked(a))
        self.assertFalse(finalize.is_draft_locked(b))
        finalize._STEP_HOOK = None
        finalize.resume_pending()
        self.assertFalse(finalize.is_draft_locked(a))

    def test_used_draft_ids_cover_issued_and_claimed(self):
        a, ref_a = self.new_draft()
        b, ref_b = self.new_draft()
        finalize.finalize(a, ref_a, PLUGIN_ROOT)
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(b, ref_b, PLUGIN_ROOT)
        self.assertEqual(finalize.used_draft_ids(), {a, b})

    def test_snapshot_survives_customer_change(self):
        draft_id, ref = self.new_draft()
        pkg = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        with open(pkg["pdf_path"], "rb") as f:
            first_pdf = f.read()
        cust = store.read_json("customers/acme.json")
        cust["address_lines"] = ["Somewhere Else 9", "1000 AA Amsterdam"]
        cust["email"] = {"to": ["new@example.com"], "cc": [], "bcc": []}
        cust["email_template"] = "Subject: changed\n\nchanged"
        store.write_json("customers/acme.json", cust)
        comp = store.read_json("company.json")
        comp["iban"] = "NL00BANK0999999999"
        store.write_json("company.json", comp)
        again = finalize.package(pkg["invoice_number"])
        self.assertNotEqual(again["pdf_path"], pkg["pdf_path"])   # a fresh handoff copy
        with open(again["pdf_path"], "rb") as f:
            self.assertEqual(f.read(), first_pdf)
        self.assertEqual(again["email"], pkg["email"])
        self.assertEqual(again["total"], pkg["total"])

    def test_package_refuses_legacy(self):
        store.write_json("invoices/INV-2026-0011.json",
                         {"invoice_number": "INV-2026-0011", "legacy": True})
        with self.assertRaises(Exception) as cm:
            finalize.package("INV-2026-0011")
        self.assertIn("lesina-invoice", str(cm.exception))
        self.assertIn("Sent", str(cm.exception))
        self.assertEqual(os.listdir(self.handoff), [])

    def test_package_unknown_number(self):
        with self.assertRaises(Exception) as cm:
            finalize.package("INV-2026-0099")
        self.assertIn("INV-2026-0099", str(cm.exception))

    def test_package_refuses_a_pdf_that_does_not_match(self):
        draft_id, ref = self.new_draft()
        pkg = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        store.write_bytes(f"invoices/{pkg['invoice_number']}.pdf", b"%PDF-1.4 other")
        with self.assertRaises(store.StoreError):
            finalize.package(pkg["invoice_number"])

    def test_publish_failure_keeps_invoice(self):
        draft_id, ref = self.new_draft()
        missing = os.path.join(self.tmp, "no-handoff")
        with mock.patch.dict(os.environ, {"CASA_HANDOFF_DIR": missing}):
            with self.assertRaises(Exception) as cm:
                finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        self.assertIn("invoice_package", str(cm.exception))
        self.assertIn("issued", str(cm.exception))
        self.assert_complete("INV-2026-0001", draft_id)
        self.assertEqual(self.files("finalizing"), [])
        # Retrying finalize, or invoice_package, publishes it.
        self.assertEqual(finalize.finalize(draft_id, ref, PLUGIN_ROOT)["invoice_number"],
                         "INV-2026-0001")
        self.assertTrue(os.path.isfile(finalize.package("INV-2026-0001")["pdf_path"]))

    def test_next_number_counts_finalizing_records(self):
        a, ref_a = self.new_draft()
        self.crash_at("finalizing")
        with self.assertRaises(Crash):
            finalize.finalize(a, ref_a, PLUGIN_ROOT)
        finalize._STEP_HOOK = None
        claim = store.read_json("finalizing/INV-2026-0001.json")
        # Even were the resume skipped, the claim holds its number.
        b, ref_b = self.new_draft(items=ITEMS[:1])
        with mock.patch.object(finalize, "resume_pending", return_value=[]):
            self.crash_at("finalizing")
            with self.assertRaises(Crash):
                finalize.finalize(b, ref_b, PLUGIN_ROOT)
        finalize._STEP_HOOK = None
        self.assertEqual(self.files("finalizing"), ["INV-2026-0001.json", "INV-2026-0002.json"])
        self.assertEqual(store.read_json("finalizing/INV-2026-0001.json"), claim)
        self.assertEqual(store.read_json("finalizing/INV-2026-0002.json")["source_draft"], b)

    def test_numbering_floor_and_year(self):
        comp = store.read_json("company.json")
        comp["numbering"] = {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 12, "seq_year": 2026}
        store.write_json("company.json", comp)
        a, ref_a = self.new_draft()
        self.assertEqual(finalize.finalize(a, ref_a, PLUGIN_ROOT)["invoice_number"],
                         "INV-2026-0012")
        b, ref_b = self.new_draft(invoice_date="2027-01-04", period="2026-12")
        self.assertEqual(finalize.finalize(b, ref_b, PLUGIN_ROOT)["invoice_number"],
                         "INV-2027-0001")

    def test_data_dir_template_change_after_approval_refused(self):
        draft_id, ref = self.new_draft()
        os.makedirs(store.path("templates"), exist_ok=True)
        with open(store.path("templates/email.md"), "w", encoding="utf-8") as f:
            f.write("Subject: {invoice_number}\n\nChanged after approval.\n")
        with self.assertRaises(finalize.RefMismatch):
            finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        self.assertEqual(self.files("invoices"), [])
        self.assertEqual(self.files("finalizing"), [])

    def test_email_rendered_from_frozen_template_not_reloaded(self):
        draft_id, ref = self.new_draft()
        content = invoice.frozen_content(
            store.read_json(f"drafts/{draft_id}.json"), COMPANY, CUSTOMER,
            email_template.load(CUSTOMER, PLUGIN_ROOT))
        self.assertIn("Subject: ", content["email_template"])
        with mock.patch.object(email_template, "load",
                               return_value=content["email_template"]) as load:
            pkg = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        rec = store.read_json(f"invoices/{pkg['invoice_number']}.json")
        self.assertEqual(rec["content"]["email_template"], content["email_template"])
        self.assertLessEqual(load.call_count, 1)    # only to freeze the content

    def test_email_from_template_at_finalize_time(self):
        os.makedirs(store.path("templates"), exist_ok=True)
        with open(store.path("templates/email.md"), "w", encoding="utf-8") as f:
            f.write("Subject: {invoice_number} for {customer_name}\n\nPay {total}.\n")
        draft_id, ref = self.new_draft()
        pkg = finalize.finalize(draft_id, ref, PLUGIN_ROOT)
        self.assertEqual(pkg["email"]["subject"], "INV-2026-0001 for Acme Biotech B.V.")
        self.assertEqual(pkg["email"]["body"], "Pay € 10.596,99.\n")
        os.unlink(store.path("templates/email.md"))
        self.assertEqual(finalize.package("INV-2026-0001")["email"], pkg["email"])


if __name__ == "__main__":
    unittest.main()
