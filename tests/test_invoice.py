# tests/test_invoice.py
"""Totals, new drafts, edits, the Ref and the summary text."""
import copy
import datetime
import pathlib
import sys
import unittest
from unittest import mock

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import invoice  # noqa: E402
import model  # noqa: E402
from model import ValidationError  # noqa: E402

COMPANY = model.validate_company({
    **copy.deepcopy(model.COMPANY_DEFAULTS),
    "name": "Example Holding B.V.",
    "address_lines": ["Keizersgracht 1", "1015 CJ Amsterdam"],
    "country": "Netherlands",
    "iban": "NL00BANK0417164300",
    "bank": "ABN AMRO",
    "tax_name": "BTW",
})
CUSTOMER = model.validate_customer({
    "id": "acme",
    "name": "Acme Biotech B.V.",
    "address_lines": ["Testlaan 2", "5678 CD Utrecht"],
    "country": "Netherlands",
    "email": {"to": ["ap@example.com"]},
    "email_template": "Subject: x\n\nbody",
})
ITEMS = [
    {"description": "Management fee", "detail": "Management services November 2026",
     "qty": "1", "unit_price": "8400.00"},
    {"description": "Travel expenses", "qty": "2", "unit_price": "178.92"},
]


def _source(n_items=7, period="2026-11"):
    items = [{"id": f"L{i}", "description": f"Item {i}", "detail": "",
              "qty": "1", "unit_price": f"{i}00.00"} for i in range(1, n_items + 1)]
    src = {"invoice_number": "INV-2026-0011", "customer_id": "acme",
           "line_items": items, "invoice_date": "2026-11-30"}
    if period is not None:
        src["period"] = period
    return src


def _fixed_now(iso="2026-12-01T09:00:00+00:00"):
    return mock.patch.object(model, "_utcnow",
                             return_value=datetime.datetime.fromisoformat(iso))


def _new(**kw):
    args = {"period": None, "invoice_date": None, "items": None}
    source = kw.pop("source", None)
    customer = kw.pop("customer", CUSTOMER)
    args.update(kw)
    return invoice.new_draft(customer, COMPANY, source, **args)


class TestTotals(unittest.TestCase):
    def test_line_total_exact(self):
        self.assertEqual(invoice.line_total_minor("1.5", 333), 500)    # 499.5 half-up
        self.assertEqual(invoice.line_total_minor("3", 1999), 5997)
        self.assertEqual(invoice.line_total_minor("0.333333", 3), 1)   # 0.999999
        self.assertEqual(invoice.line_total_minor("0.5", 1), 1)        # 0.5 -> 1
        self.assertEqual(invoice.line_total_minor("0.5", -1), -1)      # away from zero
        self.assertEqual(invoice.line_total_minor("1.5", -333), -500)
        self.assertEqual(invoice.line_total_minor("10000", 999999999999999),
                         9999999999999990000)                         # no float anywhere

    def test_vat_half_up(self):
        def items(*minors):
            return [{"id": f"L{i}", "description": "x", "detail": "", "qty": "1",
                     "unit_price": model.money.format_minor(m, "EUR")}
                    for i, m in enumerate(minors, 1)]
        t = invoice.totals(items(840000), "21", "EUR")
        self.assertEqual(t, {"lines": [840000], "subtotal": 840000,
                             "vat": 176400, "total": 1016400})
        self.assertEqual(invoice.totals(items(1), "21", "EUR")["vat"], 0)
        self.assertEqual(invoice.totals(items(3), "50", "EUR")["vat"], 2)
        t = invoice.totals(items(840000, 35784), "21", "EUR")
        self.assertEqual((t["subtotal"], t["vat"], t["total"]), (875784, 183915, 1059699))
        self.assertEqual(invoice.totals(items(1000), "12.5", "EUR")["vat"], 125)
        self.assertEqual(invoice.totals(items(1000), "0", "EUR")["vat"], 0)

    def test_totals_zero_decimal_currency(self):
        items = [{"id": "L1", "description": "x", "detail": "", "qty": "1.5", "unit_price": "333"}]
        self.assertEqual(invoice.totals(items, "10", "JPY"),
                         {"lines": [500], "subtotal": 500, "vat": 50, "total": 550})


class TestNewDraft(unittest.TestCase):
    def test_new_from_source_copies_lines_and_advances_period(self):
        src = _source()
        with _fixed_now():
            d = _new(source=src)
        self.assertEqual(d["period"], "2026-12")
        self.assertEqual(d["line_items"], src["line_items"])
        self.assertEqual(d["source_invoice"], "INV-2026-0011")
        self.assertEqual(d["customer_id"], "acme")
        self.assertEqual(d["invoice_date"], "2026-12-01")
        self.assertEqual(d["due_date"], "2026-12-31")
        self.assertEqual(d["vat_rate"], "21")
        self.assertIsNone(d["notes"])
        self.assertRegex(d["draft_id"], r"\A[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}\Z")
        # The source is not shared with the draft.
        d["line_items"][0]["qty"] = "9"
        self.assertEqual(src["line_items"][0]["qty"], "1")

    def test_new_from_finalized_record_reads_content(self):
        # A finalized invoice keeps its lines and period under "content".
        src = _source()
        record = {"invoice_number": src["invoice_number"],
                  "content": {"line_items": src["line_items"], "period": "2026-12"}}
        d = _new(source=record)
        self.assertEqual(d["period"], "2027-01")
        self.assertEqual(d["line_items"], src["line_items"])
        self.assertEqual(d["source_invoice"], "INV-2026-0011")

    def test_new_from_source_given_period_wins(self):
        d = _new(source=_source(), period="February 2027")
        self.assertEqual(d["period"], "2027-02")

    def test_new_from_source_without_period_keeps_none(self):
        d = _new(source=_source(period=None))
        self.assertIsNone(d["period"])
        self.assertEqual(len(d["line_items"]), 7)

    def test_new_from_scratch_requires_items(self):
        with self.assertRaisesRegex(ValidationError, "item"):
            _new()
        with self.assertRaisesRegex(ValidationError, "item"):
            _new(items=[])

    def test_new_from_scratch_normalizes_items(self):
        d = _new(items=ITEMS, invoice_date="2026-11-30", period="2026-11")
        self.assertEqual([i["id"] for i in d["line_items"]], ["L1", "L2"])
        self.assertEqual(d["line_items"][1],
                         {"id": "L2", "description": "Travel expenses", "detail": "",
                          "qty": "2", "unit_price": "178.92"})
        self.assertIsNone(d["source_invoice"])
        with self.assertRaisesRegex(ValidationError, "unit_price"):
            _new(items=[{"description": "x", "qty": "1", "unit_price": "13.000,00"}])

    def test_new_with_items_and_source_uses_items(self):
        d = _new(source=_source(), items=ITEMS)
        self.assertEqual(len(d["line_items"]), 2)
        self.assertEqual(d["period"], "2026-12")

    def test_source_of_another_customer_refused(self):
        other = {**_source(), "customer_id": "globex"}
        with self.assertRaisesRegex(ValidationError, "globex"):
            _new(source=other)
        record = {"invoice_number": "INV-2026-0011",
                  "content": {"customer_id": "globex", "line_items": _source()["line_items"]}}
        with self.assertRaisesRegex(ValidationError, "globex"):
            _new(source=record)
        with self.assertRaisesRegex(ValidationError, "globex"):
            _new(source=other, items=ITEMS)
        # A source that names no customer is accepted.
        anon = _source()
        del anon["customer_id"]
        self.assertEqual(_new(source=anon)["source_invoice"], "INV-2026-0011")

    def test_due_date_uses_customer_terms_override(self):
        cust = {**CUSTOMER, "payment_terms_days": 14}
        d = _new(customer=cust, items=ITEMS, invoice_date="2026-11-30")
        self.assertEqual(d["due_date"], "2026-12-14")
        cust0 = {**CUSTOMER, "payment_terms_days": 0}         # 0 is an override, not "unset"
        d = _new(customer=cust0, items=ITEMS, invoice_date="2026-11-30")
        self.assertEqual(d["due_date"], "2026-11-30")
        d = _new(items=ITEMS, invoice_date="2026-11-30")
        self.assertEqual(d["due_date"], "2026-12-30")

    def test_vat_rate_customer_override(self):
        d = _new(customer={**CUSTOMER, "vat_rate": "0"}, items=ITEMS)
        self.assertEqual(d["vat_rate"], "0")

    def test_bad_dates_and_periods_refused(self):
        for bad in ("30-11-2026", "2026-11-31", "2026-1-5"):
            with self.assertRaisesRegex(ValidationError, "invoice_date"):
                _new(items=ITEMS, invoice_date=bad)
        with self.assertRaisesRegex(ValidationError, "period"):
            _new(items=ITEMS, period="2026-13")

    def test_today_in_company_timezone(self):
        # 23:30 UTC on 30 Nov is already 1 Dec in Amsterdam.
        with _fixed_now("2026-11-30T23:30:00+00:00"):
            d = _new(items=ITEMS)
        self.assertEqual(d["invoice_date"], "2026-12-01")

    def test_draft_id_redrawn_on_collision(self):
        draws = iter(["AAAA", "BBBB", "CCCC"])
        with mock.patch.object(invoice, "_draw_id", side_effect=lambda: next(draws)):
            d = _new(items=ITEMS, existing_ids=frozenset({"AAAA", "BBBB"}))
        self.assertEqual(d["draft_id"], "CCCC")

    def test_negative_total_refused(self):
        items = ITEMS + [{"description": "Discount", "qty": "1", "unit_price": "-20000.00"}]
        with self.assertRaisesRegex(ValidationError, "credit notes are not supported"):
            _new(items=items)
        # A discount line that leaves the total >= 0 is fine.
        items[-1]["unit_price"] = "-500.00"
        d = _new(items=items)
        self.assertEqual(d["line_items"][-1]["unit_price"], "-500.00")


class TestEdits(unittest.TestCase):
    def setUp(self):
        self.draft = _new(items=ITEMS, invoice_date="2026-11-30", period="2026-11")

    def edit(self, *ops, draft=None):
        return invoice.apply_edits(draft or self.draft, list(ops), COMPANY, CUSTOMER)

    def test_edits_all_or_nothing(self):
        before = copy.deepcopy(self.draft)
        with self.assertRaisesRegex(ValidationError, "L9"):
            self.edit({"op": "change_item", "id": "L1", "field": "qty", "value": "2"},
                      {"op": "remove_item", "id": "L9"})
        self.assertEqual(self.draft, before)

    def test_edit_ops(self):
        d = self.edit(
            {"op": "change_item", "id": "L1", "field": "unit_price", "value": "13000.5"},
            {"op": "change_item", "id": "L1", "field": "qty", "value": "1.50"},
            {"op": "change_item", "id": "L1", "field": "description", "value": "  Fee "},
            {"op": "change_item", "id": "L1", "field": "detail", "value": "December"},
            {"op": "remove_item", "id": "L2"},
            {"op": "add_item", "item": {"description": "Hosting", "qty": 1, "unit_price": "50"}},
            {"op": "set", "field": "invoice_date", "value": "2026-12-01"},
            {"op": "set", "field": "notes", "value": "PO 4711"},
            {"op": "set", "field": "vat_rate", "value": "12.50"},
        )
        self.assertEqual(d["line_items"], [
            {"id": "L1", "description": "Fee", "detail": "December", "qty": "1.5",
             "unit_price": "13000.50"},
            {"id": "L2", "description": "Hosting", "detail": "", "qty": "1",
             "unit_price": "50.00"},
        ])
        self.assertEqual(d["invoice_date"], "2026-12-01")
        self.assertEqual(d["due_date"], "2026-12-31")       # recomputed, 30 days
        self.assertEqual(d["notes"], "PO 4711")
        self.assertEqual(d["vat_rate"], "12.5")
        self.assertEqual(d["draft_id"], self.draft["draft_id"])
        d = self.edit({"op": "set", "field": "period", "value": None}, draft=d)
        self.assertIsNone(d["period"])
        d = self.edit({"op": "set", "field": "period", "value": "december 2026"}, draft=d)
        self.assertEqual(d["period"], "2026-12")
        d = self.edit({"op": "set", "field": "notes", "value": None}, draft=d)
        self.assertIsNone(d["notes"])

    def test_set_invoice_date_uses_customer_terms(self):
        cust = {**CUSTOMER, "payment_terms_days": 14}
        d = invoice.apply_edits(self.draft, [{"op": "set", "field": "invoice_date",
                                              "value": "2026-12-01"}], COMPANY, cust)
        self.assertEqual(d["due_date"], "2026-12-15")

    def test_remove_last_item_refused(self):
        d = self.edit({"op": "remove_item", "id": "L2"})
        with self.assertRaisesRegex(ValidationError, "last"):
            self.edit({"op": "remove_item", "id": "L1"}, draft=d)

    def test_bad_edits_refused(self):
        bad = [
            ({"op": "change_item", "id": "L1", "field": "id", "value": "X"}, "field"),
            ({"op": "change_item", "id": "L1", "field": "unit_price", "value": "1.234,00"}, "unit_price"),
            ({"op": "change_item", "id": "L1", "field": "description", "value": ""}, "description"),
            ({"op": "change_item", "id": "L1", "field": "detail", "value": "a\nb"}, "detail"),
            ({"op": "change_item", "id": "L1", "field": "detail", "value": "☃"}, "detail"),
            ({"op": "set", "field": "notes", "value": "line\nbreak"}, "notes"),
            ({"op": "set", "field": "notes", "value": "☃"}, "notes"),
            ({"op": "set", "field": "invoice_date", "value": "2026/12/01"}, "invoice_date"),
            ({"op": "set", "field": "invoice_date", "value": None}, "invoice_date"),
            ({"op": "set", "field": "vat_rate", "value": "101"}, "VAT rate"),
            ({"op": "set", "field": "period", "value": "Brumaire 2026"}, "period"),
            ({"op": "set", "field": "due_date", "value": "2026-12-01"}, "field"),
            ({"op": "add_item", "item": {"description": "x", "qty": "1", "unit_price": "1",
                                         "id": "L1"}}, "already used"),
            ({"op": "add_item", "item": {"description": "x", "qty": "1", "price": "1"}}, "price"),
            ({"op": "rename", "id": "L1"}, "rename"),
            ({"op": "remove_item", "item_id": "L1"}, "item_id"),
            ("remove L1", "operation"),
        ]
        for op, needle in bad:
            with self.subTest(op=op):
                with self.assertRaisesRegex(ValidationError, needle):
                    self.edit(op)
        with self.assertRaises(ValidationError):
            invoice.apply_edits(self.draft, [], COMPANY, CUSTOMER)
        with self.assertRaises(ValidationError):
            invoice.apply_edits(self.draft, {"op": "remove_item", "id": "L1"}, COMPANY, CUSTOMER)

    def test_malformed_op_refused(self):
        for op in ({"op": ["set"]}, {"op": {"set": 1}}, {"op": None}, {"op": 5}, {},
                   {"op": "SET"}, ["set"], None):
            with self.subTest(op=op):
                with self.assertRaisesRegex(ValidationError, "change_item, remove_item, add_item or set"):
                    self.edit(op)

    def test_edit_to_negative_total_refused(self):
        with self.assertRaisesRegex(ValidationError, "credit notes are not supported"):
            self.edit({"op": "change_item", "id": "L1", "field": "unit_price", "value": "-8400.00"})
        d = self.edit({"op": "add_item", "item": {"description": "Discount", "qty": "1",
                                                  "unit_price": "-357.84"}})
        self.assertEqual(invoice.totals(d["line_items"], d["vat_rate"], "EUR")["subtotal"],
                         840000)


TEMPLATE = "Subject: Invoice {invoice_number}\n\nPlease pay {total}.\n"


class TestRef(unittest.TestCase):
    def setUp(self):
        self.draft = _new(items=ITEMS, invoice_date="2026-11-30", period="2026-11")

    def test_frozen_content_fields(self):
        c = invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE)
        self.assertEqual(set(c), {"company", "customer", "customer_id", "invoice_date",
                                  "due_date", "period", "line_items", "vat_rate", "notes",
                                  "source_invoice", "email_template", "totals"})
        self.assertEqual(c["email_template"], TEMPLATE)
        self.assertNotIn("numbering", c["company"])
        self.assertNotIn("email_template", c["customer"])
        self.assertEqual(c["company"]["iban"], COMPANY["iban"])
        self.assertEqual(c["totals"], {"lines": [840000, 35784], "subtotal": 875784,
                                       "vat": 183915, "total": 1059699})
        # A snapshot, not shared with the inputs.
        c["customer"]["address_lines"].append("x")
        self.assertEqual(len(CUSTOMER["address_lines"]), 2)

    def test_frozen_content_refuses_other_customer(self):
        other = {**CUSTOMER, "id": "globex"}
        with self.assertRaisesRegex(ValidationError, "globex"):
            invoice.frozen_content(self.draft, COMPANY, other, TEMPLATE)

    def test_ref_shape(self):
        ref = invoice.compute_ref(invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE))
        self.assertRegex(ref, r"\A[0-9a-f]{32}\Z")

    def test_ref_changes_when_customer_address_changes(self):
        r1 = invoice.compute_ref(invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE))
        moved = {**CUSTOMER, "address_lines": ["Other street 1", "5678 CD Utrecht"]}
        r2 = invoice.compute_ref(invoice.frozen_content(self.draft, COMPANY, moved, TEMPLATE))
        self.assertNotEqual(r1, r2)
        iban = {**COMPANY, "iban": "NL00BANK0999999999"}
        r3 = invoice.compute_ref(invoice.frozen_content(self.draft, iban, CUSTOMER, TEMPLATE))
        self.assertNotEqual(r1, r3)

    def test_ref_ignores_numbering(self):
        r1 = invoice.compute_ref(invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE))
        company = {**COMPANY, "numbering": {"pattern": "X-{seq}", "next_seq": 7, "seq_year": 2026}}
        r2 = invoice.compute_ref(invoice.frozen_content(self.draft, company, CUSTOMER, TEMPLATE))
        self.assertEqual(r1, r2)

    def test_ref_covers_the_resolved_email_template(self):
        r1 = invoice.compute_ref(invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE))
        r2 = invoice.compute_ref(invoice.frozen_content(
            self.draft, COMPANY, CUSTOMER, "Subject: other\n\nbody"))
        self.assertNotEqual(r1, r2)

    def test_ref_changes_with_draft_content(self):
        c1 = invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE)
        for op in ({"op": "set", "field": "notes", "value": "PO 1"},
                   {"op": "change_item", "id": "L2", "field": "qty", "value": "3"},
                   {"op": "set", "field": "period", "value": None}):
            d = invoice.apply_edits(self.draft, [op], COMPANY, CUSTOMER)
            c2 = invoice.frozen_content(d, COMPANY, CUSTOMER, TEMPLATE)
            self.assertNotEqual(invoice.compute_ref(c1), invoice.compute_ref(c2), op)

    def test_ref_stable_under_key_order(self):
        c = invoice.frozen_content(self.draft, COMPANY, CUSTOMER, TEMPLATE)

        def reverse(o):
            if isinstance(o, dict):
                return {k: reverse(o[k]) for k in reversed(list(o))}
            if isinstance(o, list):
                return [reverse(v) for v in o]
            return o
        r = reverse(c)
        self.assertNotEqual(list(r), list(c))
        self.assertEqual(invoice.compute_ref(c), invoice.compute_ref(r))
        rev_draft = reverse(self.draft)
        self.assertEqual(invoice.compute_ref(c), invoice.compute_ref(
            invoice.frozen_content(rev_draft, reverse(COMPANY), reverse(CUSTOMER), TEMPLATE)))

    def test_ref_known_value(self):
        # Pins the canonical form: sorted keys, compact separators, UTF-8 not escaped.
        self.assertEqual(invoice.compute_ref({"b": "€", "a": [1, None]}),
                         __import__("hashlib").sha256(
                             '{"a":[1,null],"b":"€"}'.encode("utf-8")).hexdigest()[:32])


def _cells(row: str) -> list[str]:
    """A GFM table row's cells: split on the pipes no backslash escapes."""
    assert row.startswith("| ") and row.endswith(" |"), row
    cells, cur, i = [], "", 1
    while i < len(row) - 1:
        if row[i] == "\\":
            cur += row[i:i + 2]
            i += 2
            continue
        if row[i] == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += row[i]
        i += 1
    cells.append(cur.strip())
    return cells


class TestSummary(unittest.TestCase):
    def summary(self, edits=(), customer=CUSTOMER, company=COMPANY, items=ITEMS):
        draft = _new(customer=customer, items=items, invoice_date="2026-11-30")
        if edits:
            draft = invoice.apply_edits(draft, list(edits), company, customer)
        draft["draft_id"] = "AB23"
        content = invoice.frozen_content(draft, company, customer, TEMPLATE)
        return invoice.render_summary(draft, content, "f" * 32)

    def table(self, text):
        return [line for line in text.split("\n") if line.startswith("|")]

    def test_summary_layout(self):
        draft = _new(items=ITEMS, invoice_date="2026-11-30", period="2026-11",
                     source=_source())
        draft["draft_id"] = "D7K2"
        content = invoice.frozen_content(draft, COMPANY, CUSTOMER, TEMPLATE)
        ref = "0123456789abcdef0123456789abcdef"
        expected = (
            "**Draft D7K2 — Acme Biotech B.V. — November 2026**\n"
            "Invoice date: 30-11-2026 · Due: 30-12-2026 (30 days)\n"
            "Copied from: INV-2026-0011\n"
            "\n"
            "| Item | Qty | Unit price | Amount |\n"
            "|:---|---:|---:|---:|\n"
            "| `L1` Management fee<br>Management services November 2026 | 1 | € 8.400,00 "
            "| € 8.400,00 |\n"
            "| `L2` Travel expenses | 2 | € 178,92 | € 357,84 |\n"
            "| Subtotal |  |  | € 8.757,84 |\n"
            "| BTW 21% |  |  | € 1.839,15 |\n"
            "| **Total** |  |  | **€ 10.596,99** |\n"
            "\n"
            "**Total: € 10.596,99**\n"
            "Draft: D7K2\n"
            "Ref: 0123456789abcdef0123456789abcdef"
        )
        self.assertEqual(invoice.render_summary(draft, content, ref), expected)

    def test_no_code_fence(self):
        self.assertNotIn("```", self.summary())

    def test_header_and_alignment_rows(self):
        rows = self.table(self.summary())
        self.assertEqual(rows[0], "| Item | Qty | Unit price | Amount |")
        self.assertEqual(rows[1], "|:---|---:|---:|---:|")
        # A blank line on each side keeps the table its own block.
        lines = self.summary().split("\n")
        first, last = lines.index(rows[0]), lines.index(rows[-1])
        self.assertEqual(lines[first - 1], "")
        self.assertEqual(lines[last + 1], "")

    def test_every_row_has_four_cells(self):
        text = self.summary(edits=[{"op": "add_item", "item": {
            "description": "a | b || c \\| d", "detail": "x|y", "qty": "1",
            "unit_price": "1.00"}}])
        for row in self.table(text)[2:]:
            self.assertEqual(len(_cells(row)), 4, row)

    def test_markdown_in_a_description_is_escaped(self):
        desc = "Fees | *rush* **job** [x](y) `c` ~~s~~ ==hl== $a$ <b>x</b> \\ end"
        text = self.summary(edits=[{"op": "add_item", "item": {
            "description": desc, "qty": "1", "unit_price": "1.00"}}])
        row = next(r for r in self.table(text) if "`L3`" in r)
        self.assertEqual(_cells(row)[0],
                         "`L3` Fees \\| \\*rush\\* \\*\\*job\\*\\* \\[x\\](y) \\`c\\` "
                         "\\~\\~s\\~\\~ \\=\\=hl\\=\\= \\$a\\$ \\<b>x\\</b> \\\\ end")

    def test_ampersand_escaped_only_where_it_would_form_an_entity(self):
        text = self.summary(edits=[{"op": "change_item", "id": "L2", "field": "description",
                                    "value": "R&D & travel &amp; more &#95;"}])
        self.assertIn("| `L2` R&D & travel &amp;amp; more &amp;#95; |", text)

    def test_underscores_are_left_to_casa(self):
        text = self.summary(edits=[{"op": "change_item", "id": "L2", "field": "description",
                                    "value": "snake_case_name"}])
        self.assertIn("| `L2` snake_case_name |", text)

    def test_long_description_is_kept_whole(self):
        long = "Consulting on the quarterly regulatory submission package for the EMA"
        text = self.summary(edits=[{"op": "change_item", "id": "L2", "field": "description",
                                    "value": long}])
        self.assertIn(f"| `L2` {long} | 2 | € 178,92 | € 357,84 |", text)
        self.assertNotIn("…", text)

    def test_item_with_detail(self):
        rows = self.table(self.summary())
        self.assertEqual(_cells(rows[2]), [
            "`L1` Management fee<br>Management services November 2026",
            "1", "€ 8.400,00", "€ 8.400,00"])
        self.assertEqual(_cells(rows[3])[0], "`L2` Travel expenses")

    def test_detail_is_escaped_too(self):
        text = self.summary(edits=[{"op": "change_item", "id": "L2", "field": "detail",
                                    "value": "Train A|B *2*"}])
        self.assertIn("| `L2` Travel expenses<br>Train A\\|B \\*2\\* |", text)

    def test_rows_show_ids_qty_and_unit_price_in_draft_order(self):
        text = self.summary(edits=[
            {"op": "remove_item", "id": "L1"},
            {"op": "add_item", "item": {"description": "Consulting", "qty": "1.5",
                                        "unit_price": "100.00"}}])
        rows = self.table(text)
        self.assertEqual(rows[2], "| `L2` Travel expenses | 2 | € 178,92 | € 357,84 |")
        self.assertEqual(rows[3], "| `L1` Consulting | 1,5 | € 100,00 | € 150,00 |")

    def test_vat_row(self):
        text = self.summary(edits=[{"op": "set", "field": "vat_rate", "value": "12.5"}])
        rows = self.table(text)
        self.assertEqual(rows[-3], "| Subtotal |  |  | € 8.757,84 |")
        self.assertEqual(rows[-2], "| BTW 12.5% |  |  | € 1.094,73 |")

    def test_tax_name_is_escaped(self):
        company = {**COMPANY, "tax_name": "VAT*"}
        rows = self.table(self.summary(company=company))
        self.assertEqual(rows[-2], "| VAT\\* 21% |  |  | € 1.839,15 |")

    def test_total_row_and_total_line(self):
        text = self.summary()
        self.assertEqual(self.table(text)[-1], "| **Total** |  |  | **€ 10.596,99** |")
        self.assertIn("\n\n**Total: € 10.596,99**\nDraft: AB23\nRef: " + "f" * 32, text)

    def test_dollar_amounts_are_not_read_as_math(self):
        company = {**COMPANY, "currency": "USD", "locale": "en-US"}
        rows = self.table(self.summary(company=company))
        self.assertEqual(rows[-1], "| **Total** |  |  | **\\$10,596.99** |")

    def test_customer_name_and_notes_are_escaped(self):
        customer = {**CUSTOMER, "name": "A*B | C [x]", "payment_terms_days": 1}
        text = self.summary(customer=customer, items=ITEMS[1:], edits=[
            {"op": "set", "field": "notes", "value": "PO *4711*"}])
        lines = text.split("\n")
        self.assertEqual(lines[0], "**Draft AB23 — A\\*B \\| C \\[x\\]**")
        self.assertEqual(lines[1], "Invoice date: 30-11-2026 · Due: 01-12-2026 (1 day)")
        self.assertEqual(lines[2], "Notes: PO \\*4711\\*")
        self.assertEqual(lines[3], "")
        self.assertNotIn("Copied from", text)
        self.assertTrue(text.endswith("Draft: AB23\nRef: " + "f" * 32))


if __name__ == "__main__":
    unittest.main()
