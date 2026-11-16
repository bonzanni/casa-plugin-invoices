# tests/test_email_template.py
"""Plain-text email template: validation, single-pass render, lookup order."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
import email_template  # noqa: E402
from model import ValidationError  # noqa: E402

DEFAULT = (ROOT / "templates" / "email.md").read_text(encoding="utf-8")


def content(period="2026-11", customer_name="Acme Biotech B.V."):
    return {
        "company": {"name": "Example Holding B.V.", "iban": "NL00BANK0123456789",
                    "currency": "EUR", "locale": "nl-NL"},
        "customer": {"name": customer_name},
        "customer_id": "acme",
        "invoice_date": "2026-11-15",
        "due_date": "2026-12-15",
        "period": period,
        "line_items": [],
        "vat_rate": "21",
        "notes": None,
        "source_invoice": None,
        "totals": {"lines": [], "subtotal": 1300000, "vat": 273000, "total": 1573000},
    }


class TestEmailTemplate(unittest.TestCase):

    def test_default_template_valid(self):
        email_template.validate(DEFAULT)
        self.assertTrue(DEFAULT.startswith(
            "Subject: Invoice {invoice_number} — {company_name}{period_dash}\n\n"))

    def test_values(self):
        v = email_template.email_values(content(), "INV-2026-0012")
        self.assertEqual(set(v), set(email_template.PLACEHOLDERS))
        self.assertEqual(v["total"], "€ 15.730,00")
        self.assertEqual(v["due_date"], "15-12-2026")
        self.assertEqual(v["invoice_date"], "15-11-2026")
        self.assertEqual(v["period"], "November 2026")
        self.assertEqual(v["period_dash"], " — November 2026")
        self.assertEqual(v["period_for"], " for November 2026")

    def test_render_with_period(self):
        v = email_template.email_values(content(), "INV-2026-0012")
        subject, body = email_template.render(DEFAULT, v)
        self.assertEqual(subject,
                         "Invoice INV-2026-0012 — Example Holding B.V. — November 2026")
        self.assertEqual(body, (
            "Dear Acme Biotech B.V.,\n\n"
            "Please find attached invoice INV-2026-0012 for November 2026, for a total\n"
            "of € 15.730,00, payable by 15-12-2026 to NL00BANK0123456789.\n\n"
            "Kind regards,\n"
            "Example Holding B.V.\n"))

    def test_render_without_period(self):
        v = email_template.email_values(content(period=None), "INV-2026-0012")
        self.assertEqual(v["period"], "")
        subject, body = email_template.render(DEFAULT, v)
        self.assertEqual(subject, "Invoice INV-2026-0012 — Example Holding B.V.")
        self.assertIn("invoice INV-2026-0012, for a total", body)

    def test_unknown_placeholder_refused(self):
        bad = "Subject: x\n\nPay {amount} now"
        with self.assertRaises(ValidationError) as cm:
            email_template.validate(bad)
        self.assertIn("amount", str(cm.exception))

    def test_shape_refused(self):
        for bad in ("Invoice {invoice_number}\n\nbody",
                    "Subject: x\nbody",
                    "Subject: x",
                    "Subject: {total\n\nbody",
                    "Subject: x\n\nstray } brace"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                email_template.validate(bad)
        with self.assertRaises(ValidationError):
            email_template.validate(None)

    def test_single_pass(self):
        v = email_template.email_values(content(customer_name="{iban}"), "INV-1")
        _, body = email_template.render(DEFAULT, v)
        self.assertIn("Dear {iban},", body)

    def test_literal_braces(self):
        text = "Subject: {{ref}} {invoice_number}\n\nUse {{code}} and }} too"
        email_template.validate(text)
        v = email_template.email_values(content(), "INV-9")
        subject, body = email_template.render(text, v)
        self.assertEqual(subject, "{ref} INV-9")
        self.assertEqual(body, "Use {code} and } too")

    def test_lookup_order(self):
        with tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as plugin_root:
            os.makedirs(os.path.join(plugin_root, "templates"))
            with open(os.path.join(plugin_root, "templates", "email.md"), "w",
                      encoding="utf-8") as f:
                f.write("Subject: shipped\n\nbody")
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": data}):
                self.assertEqual(email_template.load({}, plugin_root),
                                 "Subject: shipped\n\nbody")
                os.makedirs(os.path.join(data, "templates"))
                with open(os.path.join(data, "templates", "email.md"), "w",
                          encoding="utf-8") as f:
                    f.write("Subject: data\n\nbody")
                self.assertEqual(email_template.load({"email_template": ""}, plugin_root),
                                 "Subject: data\n\nbody")
                cust = {"email_template": "Subject: customer\n\nbody"}
                self.assertEqual(email_template.load(cust, plugin_root), cust["email_template"])
                with self.assertRaises(ValidationError):
                    email_template.load({"email_template": "Subject: {nope}\n\n"},
                                        plugin_root)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_DATA"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(email_template.load({}, plugin_root),
                                 "Subject: shipped\n\nbody")

    def test_invalid_data_template_refused(self):
        with tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as plugin_root:
            os.makedirs(os.path.join(data, "templates"))
            with open(os.path.join(data, "templates", "email.md"), "w",
                      encoding="utf-8") as f:
                f.write("Subject: {amount}\n\nbody")
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": data}), \
                    self.assertRaises(ValidationError) as cm:
                email_template.load({}, plugin_root)
            self.assertIn("amount", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
