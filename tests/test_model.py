# tests/test_model.py
"""Company/customer/item validation, locale formatting, dates and periods."""
import datetime
import pathlib
import sys
import unittest
from unittest import mock

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import model  # noqa: E402
from model import ValidationError  # noqa: E402

COMPANY = {
    "name": "Example B.V.",
    "address_lines": ["Keizersgracht 1", "1015 CJ Amsterdam"],
    "country": "Netherlands",
    "iban": "NL91ABNA0417164300",
    "bank": "ABN AMRO",
}
CUSTOMER = {
    "id": "acme",
    "name": "Acme Biotech B.V.",
    "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
    "country": "Netherlands",
    "email": {"to": ["ap@example.com"]},
}


class TestCompany(unittest.TestCase):
    def test_company_requires_core_fields(self):
        patch = {k: v for k, v in COMPANY.items() if k != "iban"}
        with self.assertRaises(ValidationError) as cm:
            model.merge_company(None, patch)
        self.assertIn("iban", str(cm.exception))
        with self.assertRaises(ValidationError) as cm:
            model.merge_company(None, {**COMPANY, "address_lines": []})
        self.assertIn("address_lines", str(cm.exception))

    def test_company_defaults_applied(self):
        c = model.merge_company(None, COMPANY)
        self.assertEqual(c["locale"], "nl-NL")
        self.assertEqual(c["timezone"], "Europe/Amsterdam")
        self.assertEqual(c["numbering"],
                         {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 1, "seq_year": None})
        # defaults are copied, never shared
        c["numbering"]["next_seq"] = 9
        self.assertEqual(model.COMPANY_DEFAULTS["numbering"]["next_seq"], 1)

    def test_company_partial_update_keeps_other_fields(self):
        c = model.merge_company(None, {**COMPANY, "vat_id": "NL001234567B01"})
        c2 = model.merge_company(c, {"bank": "ING", "numbering": {"next_seq": 7}})
        self.assertEqual(c2["bank"], "ING")
        self.assertEqual(c2["iban"], COMPANY["iban"])
        self.assertEqual(c2["vat_id"], "NL001234567B01")
        self.assertEqual(c2["numbering"]["pattern"], "INV-{YYYY}-{seq:4}")
        self.assertEqual(c2["numbering"]["next_seq"], 7)
        self.assertEqual(c["bank"], "ABN AMRO")  # input not mutated

    def test_company_bad_values(self):
        for patch, field in [
            ({"locale": "fr-FR"}, "locale"),
            ({"timezone": "Mars/Olympus"}, "timezone"),
            ({"default_vat_rate": "101"}, "default_vat_rate"),
            ({"payment_terms_days": -1}, "payment_terms_days"),
            ({"currency": "EURO"}, "currency"),
            ({"colour": "red"}, "colour"),
        ]:
            with self.assertRaises(ValidationError, msg=patch) as cm:
                model.merge_company(None, {**COMPANY, **patch})
            self.assertIn(field, str(cm.exception))


class TestCustomer(unittest.TestCase):
    def test_customer_email_lists(self):
        c = model.merge_customer(None, CUSTOMER)
        self.assertEqual(c["email"], {"to": ["ap@example.com"], "cc": [], "bcc": []})
        with self.assertRaises(ValidationError) as cm:
            model.merge_customer(None, {**CUSTOMER, "email": {"to": []}})
        self.assertIn("email.to", str(cm.exception))
        with self.assertRaises(ValidationError) as cm:
            model.merge_customer(None, {**CUSTOMER, "email": {"to": ["ap@example.com"],
                                                              "cc": ["not-an-address"]}})
        self.assertIn("email.cc", str(cm.exception))
        c2 = model.merge_customer(c, {"email": {"bcc": ["books@example.com"]}})
        self.assertEqual(c2["email"]["to"], ["ap@example.com"])
        self.assertEqual(c2["email"]["bcc"], ["books@example.com"])

    def test_customer_id_shape(self):
        for bad in ("ACME", "-x", "a b", "x" * 41, ""):
            with self.assertRaises(ValidationError, msg=bad) as cm:
                model.merge_customer(None, {**CUSTOMER, "id": bad})
            self.assertIn("id", str(cm.exception))

    def test_customer_requires_core_fields(self):
        with self.assertRaises(ValidationError) as cm:
            model.merge_customer(None, {k: v for k, v in CUSTOMER.items() if k != "country"})
        self.assertIn("country", str(cm.exception))

    def test_customer_optional_overrides(self):
        c = model.merge_customer(None, {**CUSTOMER, "vat_rate": "0",
                                        "vat_note": "VAT reverse-charged",
                                        "payment_terms_days": 14})
        self.assertEqual(c["vat_rate"], "0")
        with self.assertRaises(ValidationError):
            model.merge_customer(None, {**CUSTOMER, "vat_rate": "abc"})


class TestText(unittest.TestCase):
    def test_non_cp1252_text_refused(self):
        with self.assertRaises(ValidationError) as cm:
            model.merge_customer(None, {**CUSTOMER, "name": "Łódź B.V."})
        self.assertIn("'Ł'", str(cm.exception))
        self.assertIn("name", str(cm.exception))
        with self.assertRaises(ValidationError):
            model.merge_company(None, {**COMPANY, "address_lines": ["ul. Łódź 1"]})

    def test_euro_sign_allowed(self):
        self.assertEqual(model.check_text("Fee €", "description"), "Fee €")
        self.assertEqual(model.check_text("Café Müller", "name"), "Café Müller")


class TestItems(unittest.TestCase):
    def test_item_amount_strictness(self):
        for bad in ("13.000,00", "13,000.00", "abc", "1.234"):
            with self.assertRaises(ValidationError, msg=bad) as cm:
                model.normalize_item({"description": "Work", "qty": "1", "unit_price": bad},
                                     set(), "EUR")
            self.assertIn("unit_price", str(cm.exception))
        it = model.normalize_item({"description": "Work", "qty": "1", "unit_price": "13000"},
                                  set(), "EUR")
        self.assertEqual(it["unit_price"], "13000.00")
        self.assertEqual(it, {"id": "L1", "description": "Work", "detail": "",
                              "qty": "1", "unit_price": "13000.00"})

    def test_item_auto_id_and_limits(self):
        a = model.normalize_item({"description": "A", "qty": "1", "unit_price": "1"}, set(), "EUR")
        b = model.normalize_item({"description": "B", "qty": "2.5", "unit_price": "1"},
                                 {a["id"]}, "EUR")
        self.assertEqual((a["id"], b["id"]), ("L1", "L2"))
        self.assertEqual(b["qty"], "2.5")
        # gap-filling never collides with an existing id
        c = model.normalize_item({"description": "C", "qty": "1", "unit_price": "1"},
                                 {"L1", "L3"}, "EUR")
        self.assertNotIn(c["id"], {"L1", "L3"})
        for bad in ("0", "-1", "10000.5", "1.1234567", "abc", "1,5"):
            with self.assertRaises(ValidationError, msg=bad) as cm:
                model.normalize_item({"description": "A", "qty": bad, "unit_price": "1"},
                                     set(), "EUR")
            self.assertIn("qty", str(cm.exception))
        self.assertEqual(model.normalize_item({"description": "A", "qty": "10000",
                                               "unit_price": "1"}, set(), "EUR")["qty"], "10000")
        self.assertEqual(model.normalize_item({"description": "A", "qty": "0.000001",
                                               "unit_price": "1"}, set(), "EUR")["qty"],
                         "0.000001")
        full = {f"L{i}" for i in range(1, model.MAX_LINE_ITEMS + 1)}
        with self.assertRaises(ValidationError) as cm:
            model.normalize_item({"description": "A", "qty": "1", "unit_price": "1"}, full, "EUR")
        self.assertIn(str(model.MAX_LINE_ITEMS), str(cm.exception))
        with self.assertRaises(ValidationError):
            model.normalize_item({"description": "", "qty": "1", "unit_price": "1"}, set(), "EUR")
        with self.assertRaises(ValidationError):
            model.normalize_item({"id": "L1", "description": "A", "qty": "1", "unit_price": "1"},
                                 {"L1"}, "EUR")

    def test_rate_bp(self):
        self.assertEqual(model.rate_bp("21"), 2100)
        self.assertEqual(model.rate_bp("12.5"), 1250)
        self.assertEqual(model.rate_bp("0"), 0)
        self.assertEqual(model.rate_bp("100"), 10000)
        for bad in ("101", "-1", "abc", "21%", "1.234"):
            with self.assertRaises(ValidationError, msg=bad):
                model.rate_bp(bad)


    def test_item_text_limits(self):
        base = {"qty": "1", "unit_price": "1"}
        ok = model.normalize_item({**base, "description": "d" * model.MAX_DESCRIPTION,
                                   "detail": "x" * model.MAX_DETAIL}, set(), "EUR")
        self.assertEqual((len(ok["description"]), len(ok["detail"])), (200, 1000))
        for field, limit in (("description", 200), ("detail", 1000)):
            item = {**base, "description": "d", field: "y" * (limit + 1)}
            with self.assertRaises(ValidationError, msg=field) as cm:
                model.normalize_item(item, set(), "EUR")
            self.assertIn(field, str(cm.exception))
            self.assertIn(str(limit), str(cm.exception))


class TestFormatting(unittest.TestCase):
    def test_decimal_sep(self):
        self.assertEqual([model.decimal_sep(lo) for lo in model.SUPPORTED_LOCALES],
                         [",", ",", ".", "."])
        with self.assertRaises(ValidationError):
            model.decimal_sep("fr-FR")

    def test_format_money_locales(self):
        self.assertEqual(model.format_money(840000, "EUR", "nl-NL"), "€ 8.400,00")
        self.assertEqual(model.format_money(840000, "EUR", "de-DE"), "€ 8.400,00")
        self.assertEqual(model.format_money(840000, "EUR", "en-GB"), "€8,400.00")
        self.assertEqual(model.format_money(840000, "EUR", "en-US"), "€8,400.00")
        self.assertEqual(model.format_money(-500, "EUR", "nl-NL"), "-€ 5,00")
        self.assertEqual(model.format_money(-500, "EUR", "en-GB"), "-€5.00")
        self.assertEqual(model.format_money(0, "GBP", "en-GB"), "£0.00")
        self.assertEqual(model.format_money(123456789, "USD", "en-US"), "$1,234,567.89")
        self.assertEqual(model.format_money(150000, "CHF", "nl-NL"), "CHF 1.500,00")
        self.assertEqual(model.format_money(150000, "CHF", "en-GB"), "CHF 1,500.00")
        self.assertEqual(model.format_money(1500, "JPY", "en-US"), "JPY 1,500")
        with self.assertRaises(ValidationError):
            model.format_money(1, "EUR", "fr-FR")

    def test_format_date_locales(self):
        self.assertEqual(model.format_date("2026-11-15", "nl-NL"), "15-11-2026")
        self.assertEqual(model.format_date("2026-11-15", "de-DE"), "15-11-2026")
        self.assertEqual(model.format_date("2026-11-15", "en-GB"), "15/11/2026")
        self.assertEqual(model.format_date("2026-11-15", "en-US"), "11/15/2026")
        with self.assertRaises(ValidationError):
            model.format_date("2026-11-15", "xx-XX")
        with self.assertRaises(ValidationError):
            model.format_date("15-11-2026", "nl-NL")


class TestDates(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(model.period_label("2026-11"), "November 2026")
        self.assertEqual(model.next_period("2026-11"), "2026-12")
        self.assertEqual(model.next_period("2026-12"), "2027-01")
        self.assertEqual(model.parse_period("november 2026"), "2026-11")
        self.assertEqual(model.parse_period("November 2026"), "2026-11")
        self.assertEqual(model.parse_period(" 2026-11 "), "2026-11")
        for bad in ("2026-13", "Novembre 2026", "11-2026", ""):
            with self.assertRaises(ValidationError, msg=bad):
                model.parse_period(bad)
        with self.assertRaises(ValidationError):
            model.period_label("2026-00")

    def test_today_uses_timezone(self):
        fixed = datetime.datetime(2026, 11, 30, 23, 30, tzinfo=datetime.timezone.utc)
        with mock.patch.object(model, "_utcnow", return_value=fixed):
            self.assertEqual(model.today("Europe/Amsterdam"), "2026-12-01")
            self.assertEqual(model.today("UTC"), "2026-11-30")
        with self.assertRaises(ValidationError):
            model.today("Nowhere/City")

    def test_parse_date(self):
        self.assertEqual(model.parse_date("2026-11-15"), datetime.date(2026, 11, 15))
        for bad in ("2026-02-30", "15-11-2026", "2026-1-5", "2026-11-15T00:00", None, 20261115):
            with self.assertRaisesRegex(ValidationError, "date", msg=repr(bad)):
                model.parse_date(bad)
        with self.assertRaisesRegex(ValidationError, "invoice_date"):
            model.parse_date("x", "invoice_date")

    def test_add_days(self):
        self.assertEqual(model.add_days("2026-11-15", 30), "2026-12-15")
        self.assertEqual(model.add_days("2026-12-15", 30), "2027-01-14")
        with self.assertRaises(ValidationError):
            model.add_days("2026-02-30", 1)


class TestReviewFixes(unittest.TestCase):
    def test_item_unknown_keys_refused(self):
        with self.assertRaises(ValidationError) as cm:
            model.normalize_item({"description": "d", "qty": "1", "unit_price": "1",
                                  "vat_rate": "9"}, set(), "EUR")
        self.assertIn("vat_rate", str(cm.exception))

    def test_canonical_rate(self):
        for raw, want in [("21", "21"), ("12.5", "12.5"), ("0.05", "0.05"), ("0", "0"),
                          ("+021.0", "21"), ("20.00", "20"), ("100", "100")]:
            self.assertEqual(model.canonical_rate(raw), want, raw)
        with self.assertRaises(ValidationError):
            model.canonical_rate("101")
        c = model.merge_company(None, {**COMPANY, "default_vat_rate": "+021.0"})
        self.assertEqual(c["default_vat_rate"], "21")
        cu = model.merge_customer(None, {**CUSTOMER, "vat_rate": "09.00"})
        self.assertEqual(cu["vat_rate"], "9")

    def test_dates_strict_shape(self):
        for bad in ("2026-W46-7", "20261115", "2026-11-15T00:00", "２０２６-11-15"):
            with self.assertRaises(ValidationError, msg=bad):
                model.format_date(bad, "nl-NL")
            with self.assertRaises(ValidationError, msg=bad):
                model.add_days(bad, 1)

    def test_parse_period_ascii_digits_only(self):
        for bad in ("November ２０２６", "２０２６-11", "2026-１１"):
            with self.assertRaises(ValidationError, msg=bad):
                model.parse_period(bad)

    def test_control_characters_refused(self):
        for bad in ("a\nb", "a\tb", "a\x00b", "a\x7fb", "a\rb"):
            with self.assertRaises(ValidationError, msg=repr(bad)) as cm:
                model.check_text(bad, "description")
            self.assertIn("description", str(cm.exception))
        with self.assertRaises(ValidationError):
            model.merge_customer(None, {**CUSTOMER, "name": "Acme\nB.V."})


if __name__ == "__main__":
    unittest.main()
