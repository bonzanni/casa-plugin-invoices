# tests/test_pdf.py
"""The stdlib PDF writer and the invoice layout, checked by parsing the output."""
import copy
import pathlib
import re
import sys
import unittest

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoice  # noqa: E402
import invoice_pdf  # noqa: E402
import model  # noqa: E402
import pdf  # noqa: E402
import pdfparse  # noqa: E402

COMPANY = model.validate_company({
    **copy.deepcopy(model.COMPANY_DEFAULTS),
    "name": "Example Holding B.V.",
    "address_lines": ["Keizersgracht 1", "1015 CJ Amsterdam"],
    "country": "Netherlands",
    "vat_id": "NL123456789B01",
    "registration_id": "12345678",
    "vat_label": "BTW-id",
    "registration_label": "KvK",
    "email": "admin@example.com",
    "iban": "NL00BANK0417164300",
    "bank": "ABN AMRO",
    "bic": "ABNANL2A",
    "tax_name": "BTW",
})
CUSTOMER = model.validate_customer({
    "id": "acme",
    "name": "Acme Biotech B.V.",
    "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
    "country": "Netherlands",
    "vat_id": "NL987654321B01",
    "email": {"to": ["ap@example.com"]},
})
ITEMS = [
    {"description": "Management fee", "detail": "Management services November 2026",
     "qty": "1", "unit_price": "8400.00"},
    {"description": "Travel expenses", "qty": "2", "unit_price": "178.92"},
]


def _content(items=ITEMS, customer=CUSTOMER, **draft_overrides):
    draft = invoice.new_draft(customer, COMPANY, None, period="2026-11",
                              invoice_date="2026-11-30", items=copy.deepcopy(items))
    draft.update(draft_overrides)
    return invoice.frozen_content(draft, COMPANY, customer, "Subject: x\n\nbody\n")


def _strings(data):
    return [t["text"] for t in pdfparse.texts(data)]


class TestWriter(unittest.TestCase):
    def test_minimal_document_is_valid_pdf(self):
        doc = pdf.Document()
        doc.new_page()
        doc.text(71, 57, "Hello", size=12)
        for compress in (True, False):
            data = doc.to_bytes(compress=compress)
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))
            start = int(re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data).group(1))
            self.assertEqual(data[start:start + 4], b"xref")
            xref = data[start:].split(b"trailer")[0].split(b"\n")
            count = int(xref[1].split()[1])
            entries = xref[2:2 + count]
            self.assertTrue(entries[0].startswith(b"0000000000 65535 f"))
            for num, entry in enumerate(entries[1:], start=1):
                self.assertEqual(len(entry) + 1, 20)            # 20-byte xref lines
                off = int(entry.split()[0])
                self.assertTrue(data[off:].startswith(b"%d 0 obj" % num), num)
            self.assertEqual(int(re.search(rb"/Size (\d+)", data).group(1)), count)
            self.assertEqual(pdfparse.page_count(data), 1)
            self.assertEqual(_strings(data), ["Hello"])

    def test_euro_encoded_winansi(self):
        doc = pdf.Document()
        doc.new_page()
        doc.text(71, 57, "€ 5,00")
        data = doc.to_bytes(compress=False)
        self.assertIn(b"(\x80 5,00) Tj", data)
        self.assertIn(b"/Encoding /WinAnsiEncoding", data)
        self.assertEqual(_strings(data), ["€ 5,00"])

    def test_text_outside_cp1252_refused(self):
        doc = pdf.Document()
        doc.new_page()
        with self.assertRaises(ValueError):
            doc.text(71, 57, "Łódź")

    def test_escaping(self):
        doc = pdf.Document()
        doc.new_page()
        doc.text(71, 57, "(a) \\ b")
        data = doc.to_bytes(compress=False)
        self.assertIn(b"(\\(a\\) \\\\ b) Tj", data)
        self.assertEqual(_strings(doc.to_bytes()), ["(a) \\ b"])

    def test_y_from_top(self):
        doc = pdf.Document()
        doc.new_page()
        doc.text(71, 100, "X", size=10)
        t = pdfparse.texts(doc.to_bytes())[0]
        self.assertAlmostEqual(t["y"], pdf.PAGE_H - 100 - 7.18, places=2)  # baseline = top + ascent
        self.assertAlmostEqual(t["x"], 71)

    def test_text_right_aligns(self):
        doc = pdf.Document()
        doc.new_page()
        doc.text_right(500, 100, "HH", size=10)
        t = pdfparse.texts(doc.to_bytes())[0]
        self.assertAlmostEqual(t["x"], 500 - 14.44, places=2)

    def test_text_width_known_values(self):
        self.assertEqual(pdf.text_width("H", "F1", 1000), 722)
        self.assertEqual(pdf.text_width(" ", "F1", 1000), 278)
        self.assertEqual(pdf.text_width("€", "F1", 1000), 556)
        self.assertEqual(pdf.text_width("H", "F2", 1000), 722)
        self.assertEqual(pdf.text_width("i", "F2", 1000), 278)
        self.assertEqual(pdf.text_width("i", "F1", 1000), 222)
        self.assertAlmostEqual(pdf.text_width("Hi", "F1", 10), 9.44)
        self.assertEqual(sorted(pdf.HELVETICA_WIDTHS), list(range(32, 256)))
        self.assertEqual(sorted(pdf.HELVETICA_BOLD_WIDTHS), list(range(32, 256)))

    def test_wrap(self):
        self.assertEqual(pdf.wrap("", "F1", 10, 100), [""])
        self.assertEqual(pdf.wrap("aaa bbb ccc", "F1", 10, 1000), ["aaa bbb ccc"])
        lines = pdf.wrap("aaa bbb ccc", "F1", 10, pdf.text_width("aaa bbb", "F1", 10))
        self.assertEqual(lines, ["aaa bbb", "ccc"])
        long = "x" * 100
        lines = pdf.wrap(long, "F1", 10, 50)
        self.assertEqual("".join(lines), long)
        self.assertTrue(all(pdf.text_width(s, "F1", 10) <= 50 for s in lines))
        self.assertGreater(len(lines), 1)

    def test_rotated_text_and_shapes(self):
        doc = pdf.Document()
        doc.new_page()
        doc.rect(10, 10, 100, 20, fill=(0.5, 0.5, 0.5))
        doc.line(10, 40, 100, 40, 1, (0, 0, 0))
        doc.rotated_text(100, 400, "DRAFT", 45, 80, (0.8, 0.8, 0.8))
        doc.new_page()
        data = doc.to_bytes()
        self.assertEqual(pdfparse.page_count(data), 2)
        self.assertEqual(_strings(data), ["DRAFT"])


class TestInvoicePdf(unittest.TestCase):
    def test_invoice_contains_key_texts(self):
        content = _content()
        data = invoice_pdf.render_invoice(content, "INV-2026-0012", draft=False)
        s = _strings(data)
        self.assertIn("INV-2026-0012", s)
        self.assertIn("Example Holding B.V.", s)
        self.assertIn("Acme Biotech B.V.", s)
        self.assertIn("€ 10.596,99", s)
        self.assertIn("€ 8.757,84", s)
        self.assertIn("€ 1.839,15", s)
        self.assertIn("€ 178,92", s)                        # unit price
        self.assertIn("€ 357,84", s)                        # line total from content
        self.assertIn("IBAN: NL00BANK0417164300", s)
        self.assertIn("Reference: INV-2026-0012", s)
        self.assertIn("Account holder: Example Holding B.V.", s)
        self.assertIn("BIC: ABNANL2A", s)
        self.assertIn("Bank: ABN AMRO", s)
        self.assertIn("BTW-id: NL123456789B01", s)
        self.assertIn("KvK: 12345678", s)
        self.assertIn("BTW-id: NL987654321B01", s)
        self.assertIn("BTW 21%:", s)
        self.assertIn("All amounts are exclusive of 21% BTW", s)
        self.assertIn("30-11-2026", s)
        self.assertIn("30-12-2026", s)
        self.assertIn("November 2026", s)
        self.assertNotIn("DRAFT", s)
        self.assertEqual(pdfparse.page_count(data), 1)

    def test_amounts_come_from_content_totals(self):
        content = _content()
        content["totals"]["total"] = 1                      # never recomputed
        s = _strings(invoice_pdf.render_invoice(content, "INV-2026-0012", draft=False))
        self.assertIn("€ 0,01", s)
        self.assertNotIn("€ 10.596,99", s)

    def test_locale_qty_and_rate(self):
        items = [{"description": "Hours", "qty": "1.5", "unit_price": "100.00"}]
        content = _content(items, vat_rate="12.5")
        s = _strings(invoice_pdf.render_invoice(content, "INV-1", draft=False))
        self.assertIn("1,5", s)
        self.assertIn("BTW 12,5%:", s)
        content["company"]["locale"] = "en-GB"
        s = _strings(invoice_pdf.render_invoice(content, "INV-1", draft=False))
        self.assertIn("1.5", s)
        self.assertIn("BTW 12.5%:", s)
        self.assertIn("€150.00", s)

    def test_notes_on_pdf(self):
        content = _content(notes="Please quote the PO number 4711 with your payment. " * 4)
        s = " ".join(_strings(invoice_pdf.render_invoice(content, "INV-1", draft=False)))
        self.assertIn("PO number 4711", s)

    def test_draft_mark(self):
        data = invoice_pdf.render_invoice(_content(), None, draft=True)
        texts = pdfparse.texts(data)
        s = [t["text"] for t in texts]
        self.assertIn("DRAFT", s)
        self.assertGreaterEqual(s.count("DRAFT"), 2)        # header + diagonal mark
        self.assertFalse(any("INV-" in t for t in s))
        big = [t for t in texts if t["text"] == "DRAFT" and t["size"] >= 60]
        self.assertEqual(len(big), pdfparse.page_count(data))

    def test_final_needs_number(self):
        with self.assertRaises(ValueError):
            invoice_pdf.render_invoice(_content(), None, draft=False)

    def test_many_items_page_break(self):
        items = [{"description": f"Consulting block {i}", "detail": ("detail " * 20)[:120],
                  "qty": "1", "unit_price": "10.00"} for i in range(50)]
        data = invoice_pdf.render_invoice(_content(items), "INV-2026-0012", draft=False)
        texts = pdfparse.texts(data)
        n = pdfparse.page_count(data)
        self.assertGreaterEqual(n, 2)
        totals = [t for t in texts if t["text"] == "Total:"]
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["page"], n - 1)
        headers = [t for t in texts if t["text"] == "DESCRIPTION"]
        item_pages = {t["page"] for t in texts if t["text"].startswith("Consulting block")}
        self.assertGreaterEqual(len(item_pages), 2)
        self.assertEqual({t["page"] for t in headers}, item_pages)   # header repeated
        separator_y = pdf.PAGE_H - (invoice_pdf.FOOTER_Y - 10)
        footer = [t for t in texts if t["y"] < separator_y]
        self.assertEqual(len(footer), 3 * n)                # two footer lines + page label
        labels = [t["text"] for t in footer if "Page" in t["text"]]
        self.assertEqual(labels, [f"Invoice INV-2026-0012  ·  Page {i} of {n}"
                                  for i in range(1, n + 1)])
        self.assertTrue(all(t["size"] <= 7 for t in footer))
        for t in texts:
            if t not in footer:
                self.assertGreater(t["y"] - 3, separator_y, t)   # descenders clear the line
        details = [t["text"] for t in texts if t["text"].startswith("detail")]
        self.assertGreater(len(details), 50)               # 120-char details wrapped

    def test_longest_items_stay_above_footer(self):
        words = " ".join(f"word{i % 97}" for i in range(400))
        items = [{"description": ("Longest description " * 20)[:model.MAX_DESCRIPTION],
                  "detail": (words if i % 2 else "W" * 2000)[:model.MAX_DETAIL],
                  "qty": "1", "unit_price": "10.00"} for i in range(6)]
        data = invoice_pdf.render_invoice(_content(items), "INV-1", draft=False)
        texts = pdfparse.texts(data)
        separator_y = pdf.PAGE_H - (invoice_pdf.FOOTER_Y - 10)
        body = [t for t in texts if t["size"] > 7 or t["text"].startswith("All amounts")]
        self.assertGreater(len(body), 100)
        for t in body:
            self.assertGreater(t["y"] - 3, separator_y, t)
            self.assertLess(t["y"], pdf.PAGE_H, t)
        detail_lines = [t for t in texts if t["size"] == 8 and t["text"].startswith("W")]
        self.assertEqual(sum(len(t["text"]) for t in detail_lines), 3 * model.MAX_DETAIL)

    def test_single_page_has_no_page_label(self):
        s = _strings(invoice_pdf.render_invoice(_content(), "INV-1", draft=False))
        self.assertFalse(any("Page" in t for t in s))

    def test_draft_page_label(self):
        items = [{"description": f"Item {i}", "qty": "1", "unit_price": "1.00"} for i in range(50)]
        data = invoice_pdf.render_invoice(_content(items), None, draft=True)
        n = pdfparse.page_count(data)
        self.assertGreaterEqual(n, 2)
        labels = [t for t in _strings(data) if "Page" in t]
        self.assertEqual(labels, [f"Draft invoice  ·  Page {i} of {n}" for i in range(1, n + 1)])

    def test_vat_row_clears_the_rule_below(self):
        texts = pdfparse.texts(invoice_pdf.render_invoice(_content(), "INV-1", draft=False))
        vat = next(t for t in texts if t["text"] == "BTW 21%:")
        total = next(t for t in texts if t["text"] == "Total:")
        # descender (~0.21 em) to the 1.5pt rule: at least 4pt of air
        self.assertGreater(vat["y"] - 0.21 * 9 - (total["y"] + 0.718 * 13 + 16), 4)

    def _assert_no_overlap(self, data):
        """No two texts on one baseline overlap horizontally."""
        texts = pdfparse.texts(data)
        for a in texts:
            for b in texts:
                if a is b or a["page"] != b["page"] or abs(a["y"] - b["y"]) > a["size"]:
                    continue
                if a["x"] < b["x"]:
                    end = a["x"] + pdf.text_width(a["text"], a["font"], a["size"])
                    self.assertLess(end + 4, b["x"], (a["text"], b["text"]))

    def test_default_columns_unchanged_for_ordinary_numbers(self):
        texts = pdfparse.texts(invoice_pdf.render_invoice(_content(), "INV-1", draft=False))
        qty = next(t for t in texts if t["text"] == "QTY")
        self.assertAlmostEqual(qty["x"] + pdf.text_width("QTY", "F2", 8),
                               invoice_pdf.COL_QTY_R, places=2)

    def test_large_numbers_do_not_overlap(self):
        items = [
            {"description": "Six-digit price", "qty": "1", "unit_price": "999999.99"},
            {"description": "Max qty", "qty": "10000", "unit_price": "999999.99"},
            {"description": "Fractional qty " * 5, "qty": "9999.999999", "unit_price": "123456.78"},
        ]
        self._assert_no_overlap(invoice_pdf.render_invoice(_content(items), "INV-1", draft=False))

    def test_largest_valid_numbers_do_not_overlap(self):
        items = ([{"description": "Largest " * 25, "qty": "10000",
                   "unit_price": "9999999999999.99"}] * 49        # 50 lines: widest total
                 + [{"description": "Discount", "qty": "9999.999999",
                     "unit_price": "-9999999999999.99"}])
        data = invoice_pdf.render_invoice(_content(items), "INV-1", draft=False)
        self._assert_no_overlap(data)
        total = next(t for t in pdfparse.texts(data) if t["text"].startswith("€ ")
                     and t["font"] == "F2")
        self.assertLess(total["size"], 13)                 # the total shrank to fit

    def test_long_tax_name_wraps_and_amounts_stay_full_size(self):
        global COMPANY
        saved = COMPANY
        try:
            COMPANY = model.validate_company({**saved, "tax_name": "Belasting toegevoegde waarde"})
            data = invoice_pdf.render_invoice(_content(), "INV-1", draft=False)
        finally:
            COMPANY = saved
        self._assert_no_overlap(data)
        texts = pdfparse.texts(data)
        for amount in ("€ 8.757,84", "€ 1.839,15"):
            self.assertEqual(next(t for t in texts if t["text"] == amount)["size"], 9)
        right = invoice_pdf.COL_AMOUNT_R - pdf.text_width("€ 8.757,84", "F1", 9) - 12
        labels = [t for t in texts if t["x"] == round(invoice_pdf.LEFT + invoice_pdf.CONTENT_W
                                                      * 0.55, 3) and t["size"] == 9]
        self.assertGreater(len(labels), 2)                  # labels wrapped
        for t in labels:
            self.assertLessEqual(t["x"] + pdf.text_width(t["text"], "F1", 9), right + 0.01)

    def test_tax_name_length_capped(self):
        with self.assertRaises(model.ValidationError):
            model.validate_company({**COMPANY, "tax_name": "X" * (model.MAX_TAX_NAME + 1)})

    def test_page_label_fits_a_long_invoice_number(self):
        items = [{"description": f"Item {i}", "qty": "1", "unit_price": "1.00"} for i in range(50)]
        number = "INV-" + "9" * 156
        texts = pdfparse.texts(invoice_pdf.render_invoice(_content(items), number, draft=False))
        labels = [t for t in texts if "Page" in t["text"]]
        self.assertGreaterEqual(len(labels), 2)
        for t in labels:
            self.assertGreaterEqual(t["x"], invoice_pdf.LEFT - 0.1)
            self.assertLessEqual(t["x"] + pdf.text_width(t["text"], "F2", t["size"]),
                                 invoice_pdf.RIGHT + 0.1)   # size is written to 3 decimals

    def test_lines_and_totals_must_pair(self):
        content = _content()
        content["totals"]["lines"].pop()
        with self.assertRaises(ValueError):
            invoice_pdf.render_invoice(content, "INV-1", draft=False)

    def test_zero_rate_uses_vat_note(self):
        customer = model.validate_customer({
            **CUSTOMER, "vat_rate": "0", "vat_note": "VAT reverse-charged (Art. 196 VAT Directive)"})
        content = _content(customer=customer)
        s = _strings(invoice_pdf.render_invoice(content, "INV-1", draft=False))
        self.assertIn("VAT reverse-charged (Art. 196 VAT Directive)", s)
        self.assertFalse(any(t.startswith("All amounts are exclusive") for t in s))
        self.assertIn("BTW 0%:", s)
        self.assertIn("€ 0,00", s)


if __name__ == "__main__":
    unittest.main()
