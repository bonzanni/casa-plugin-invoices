"""The invoice layout: frozen content -> PDF bytes.

Ported from the n8n pdfkit node of casa-plugin-lesina-invoice
(docs/n8n/invoice-pdf-code-node.js @ 7179708): same page geometry, colours and
sections, with every company, customer, tax and rate value taken from the
content. Amounts are never recomputed here: they come from content["totals"]
(unit prices from the line items), formatted for the company's locale.
"""
from __future__ import annotations

import money
import model
import pdf

MARGIN_L = MARGIN_R = 71
MARGIN_T = MARGIN_B = 57
LEFT = MARGIN_L
RIGHT = pdf.PAGE_W - MARGIN_R
CONTENT_W = RIGHT - LEFT
FOOTER_Y = pdf.PAGE_H - MARGIN_B - 10        # top of footer line 1
CONTENT_BOTTOM = FOOTER_Y - 16               # flowing content stays above this


def _hex(h: str) -> tuple[float, float, float]:
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


PRIMARY = _hex("#1a1a2e")
ACCENT = _hex("#0f4c75")
LIGHT_BG = _hex("#f8f9fa")
BORDER = _hex("#dee2e6")
TEXT_DARK = _hex("#212529")
TEXT_MUTED = _hex("#6c757d")
WHITE = (1, 1, 1)
DRAFT_GREY = (0.6, 0.6, 0.6)

COL2_X = LEFT + 269                          # meta block / customer ids
META_VAL_X = COL2_X + 108
COL_DESC_L = LEFT + 12
COL_QTY_R = LEFT + 284
COL_PRICE_R = LEFT + 369
COL_AMOUNT_R = RIGHT - 12
DESC_W = COL_QTY_R - 45 - COL_DESC_L - 8     # description column, clear of QTY
HEADER_H = 24


class _Layout:
    def __init__(self, content: dict, number: str, draft: bool):
        self.c = content
        self.company = content["company"]
        self.customer = content["customer"]
        self.number = number
        self.draft = draft
        self.locale = self.company["locale"]
        self.doc = pdf.Document(info={
            "Title": f"Invoice {number}",
            "Author": self.company["name"],
            "Subject": f"Invoice {number} - {self.company['name']} to {self.customer['name']}",
        })
        self.y = MARGIN_T
        self._start_page()

    # ---------------------------------------------------------------- helpers

    def money(self, minor: int) -> str:
        return model.format_money(minor, self.company["currency"], self.locale)

    def decimal(self, canonical: str) -> str:
        """A canonical "1.5" with the locale's decimal separator."""
        return canonical.replace(".", model.decimal_sep(self.locale))

    def text(self, x, y, s, font="F1", size=9, color=TEXT_DARK):
        self.doc.text(x, y, s, font=font, size=size, color=color)

    def right(self, x, y, s, font="F1", size=9, color=TEXT_DARK):
        self.doc.text_right(x, y, s, font=font, size=size, color=color)

    def _start_page(self):
        self.doc.new_page()
        self.y = MARGIN_T
        self._footer()

    def _finish_page(self):
        if self.draft:                           # on top of everything, translucent
            d, size, angle = self.doc, 110, 45
            w = pdf.text_width("DRAFT", "F2", size)
            cap = 0.718 * size
            # baseline start so the word's centre sits on the page centre (y from top)
            x = pdf.PAGE_W / 2 - w / 2 * 0.7071 + cap / 2 * 0.7071
            y = pdf.PAGE_H / 2 + w / 2 * 0.7071 + cap / 2 * 0.7071
            d.rotated_text(x, y, "DRAFT", angle, size, DRAFT_GREY, opacity=0.18)

    def new_page(self):
        self._finish_page()
        self._start_page()

    def ensure_space(self, needed: float) -> bool:
        """Start a new page when `needed` points would run into the footer."""
        if self.y + needed > CONTENT_BOTTOM:
            self.new_page()
            return True
        return False

    # ---------------------------------------------------------------- sections

    def _footer(self):
        co = self.company
        self.doc.line(LEFT, FOOTER_Y - 10, RIGHT, FOOTER_Y - 10, 0.5, BORDER)
        parts = [co["name"], ", ".join(co["address_lines"])]
        if co.get("registration_id"):
            parts.append(f"{co['registration_label']}: {co['registration_id']}")
        if co.get("vat_id"):
            parts.append(f"{co['vat_label']}: {co['vat_id']}")
        if co.get("email"):
            parts.append(co["email"])
        bank = f"IBAN: {co['iban']} ({co['bank']})"
        if co.get("bic"):
            bank += f"  ·  BIC: {co['bic']}"
        for n, line in enumerate(("  ·  ".join(parts), bank)):
            size = min(7, 7 * CONTENT_W / max(pdf.text_width(line, "F1", 7), 1))
            self.doc.text_center(LEFT + CONTENT_W / 2, FOOTER_Y + 10 * n, line,
                                 size=size, color=TEXT_MUTED)

    def header(self):
        name, y = self.company["name"], self.y
        size = 22
        while size > 12 and pdf.text_width(name, "F2", size) > CONTENT_W - 130:
            size -= 1
        self.text(LEFT, y, name, "F2", size, PRIMARY)
        self.right(RIGHT, y + 2, "INVOICE", "F1", 10, TEXT_MUTED)
        self.right(RIGHT, y + 16, self.number, "F2", 14, PRIMARY)
        self.y += 34
        self.doc.line(LEFT, self.y, RIGHT, self.y, 1, ACCENT)
        self.y += 16

    def parties_from(self):
        co, c = self.company, self.c
        y0 = self.y
        sy = y0 + 20
        self.text(LEFT, sy, "From:", "F2", 8, TEXT_MUTED)
        width = COL2_X - LEFT - 12
        for line in pdf.wrap(co["name"], "F2", 10, width):
            sy += 14
            self.text(LEFT, sy, line, "F2", 10)
        sy += 2
        for raw in co["address_lines"] + [co["country"]]:
            for line in pdf.wrap(raw, "F1", 9, width):
                sy += 12
                self.text(LEFT, sy, line)
        sy += 4
        for label, key in (("vat_label", "vat_id"), ("registration_label", "registration_id")):
            if co.get(key):
                sy += 12
                self.text(LEFT, sy, f"{co[label]}: {co[key]}", size=8, color=TEXT_MUTED)

        terms = (model.parse_date(c["due_date"]) - model.parse_date(c["invoice_date"])).days
        meta = [("Invoice date:", model.format_date(c["invoice_date"], self.locale)),
                ("Due date:", model.format_date(c["due_date"], self.locale)),
                ("Payment terms:", f"{terms} days")]
        if c.get("period"):
            meta.append(("Period:", model.period_label(c["period"])))
        my = y0 + 20
        for label, value in meta:
            self.text(COL2_X, my, label, size=8, color=TEXT_MUTED)
            self.text(META_VAL_X, my, value, "F2", 9)
            my += 16
        self.y = max(sy, my) + 16

    def parties_to(self):
        co, cu, y = self.company, self.customer, self.y
        width = COL2_X - LEFT - 24
        rows = [(10, "F2", 8, "To:", TEXT_MUTED)]           # (offset in box, font, size, text, colour)
        off = 10
        for line in pdf.wrap(cu["name"], "F2", 10, width):
            off += 14
            rows.append((off, "F2", 10, line, TEXT_DARK))
        addr = list(cu["address_lines"])
        addr[-1] = f"{addr[-1]}, {cu['country']}"
        step = 14
        for raw in addr:
            for line in pdf.wrap(raw, "F1", 9, width):
                off += step
                step = 12
                rows.append((off, "F1", 9, line, TEXT_DARK))
        box_h = max(80, off + 30)
        self.doc.rect(LEFT, y, CONTENT_W, box_h, fill=LIGHT_BG, stroke=BORDER, radius=3)
        for off, font, size, line, color in rows:
            self.text(LEFT + 12, y + off, line, font, size, color)
        iy = y + 38
        for label, key in (("vat_label", "vat_id"), ("registration_label", "registration_id")):
            if cu.get(key):
                self.text(COL2_X, iy, f"{co[label]}: {cu[key]}", size=8, color=TEXT_MUTED)
                iy += 12
        self.y += box_h + 12

    def table_header(self):
        y = self.y
        self.doc.rect(LEFT, y, CONTENT_W, HEADER_H, fill=PRIMARY, radius=2)
        ty = y + 8
        self.text(COL_DESC_L, ty, "DESCRIPTION", "F2", 8, WHITE)
        self.right(COL_QTY_R, ty, "QTY", "F2", 8, WHITE)
        self.right(COL_PRICE_R, ty, "UNIT PRICE", "F2", 8, WHITE)
        self.right(COL_AMOUNT_R, ty, "AMOUNT", "F2", 8, WHITE)
        self.y += HEADER_H + 4

    def items(self):
        currency = self.company["currency"]
        self.table_header()
        for i, (item, line_minor) in enumerate(zip(self.c["line_items"],
                                                   self.c["totals"]["lines"], strict=True)):
            desc = pdf.wrap(item["description"], "F2", 9, DESC_W)
            detail = pdf.wrap(item["detail"], "F1", 8, DESC_W) if item.get("detail") else []
            last_desc = 8 + 11 * (len(desc) - 1)
            row_h = (last_desc + 14 + 10 * (len(detail) - 1) + 14) if detail else last_desc + 20
            if self.ensure_space(row_h):
                self.table_header()
            y = self.y
            if i % 2 == 0:
                self.doc.rect(LEFT, y, CONTENT_W, row_h, fill=LIGHT_BG)
            ty = y + 8
            for n, line in enumerate(desc):
                self.text(COL_DESC_L, ty + 11 * n, line, "F2", 9)
            for n, line in enumerate(detail):
                self.text(COL_DESC_L, ty + last_desc - 8 + 14 + 10 * n, line, size=8,
                          color=TEXT_MUTED)
            self.right(COL_QTY_R, ty, self.decimal(item["qty"]))
            self.right(COL_PRICE_R, ty, self.money(money.to_minor(item["unit_price"], currency)))
            self.right(COL_AMOUNT_R, ty, self.money(line_minor))
            self.y += row_h

    def totals(self):
        c, co = self.c, self.company
        tax, rate = co["tax_name"], self.decimal(c["vat_rate"])
        note = self.customer.get("vat_note") if model.rate_bp(c["vat_rate"]) == 0 else None
        note = note or f"All amounts are exclusive of {rate}% {tax}"
        note_lines = pdf.wrap(note, "F1", 7, CONTENT_W - 24)
        # VAT note + totals stay together, so a page break cannot split them
        self.ensure_space(6 + 9 * (len(note_lines) - 1) + 12 + 16 + 18 + 10 + 16 + 30)
        self.y += 6
        for n, line in enumerate(note_lines):
            self.text(LEFT + 12, self.y + 9 * n, line, size=7, color=TEXT_MUTED)
        self.y += 9 * (len(note_lines) - 1) + 12
        self.doc.line(LEFT + CONTENT_W * 0.5, self.y, RIGHT, self.y, 0.5, BORDER)
        self.y += 16

        label_x = LEFT + CONTENT_W * 0.55
        t = c["totals"]
        self.text(label_x, self.y, f"Subtotal excl. {tax}:", color=TEXT_MUTED)
        self.right(COL_AMOUNT_R, self.y, self.money(t["subtotal"]))
        self.y += 18
        self.text(label_x, self.y, f"{tax} {rate}%:", color=TEXT_MUTED)
        self.right(COL_AMOUNT_R, self.y, self.money(t["vat"]))
        self.y += 10
        self.doc.line(label_x, self.y, RIGHT, self.y, 1.5, PRIMARY)
        self.y += 16
        self.text(label_x, self.y, "Total:", "F2", 13, PRIMARY)
        self.right(COL_AMOUNT_R, self.y, self.money(t["total"]), "F2", 13, PRIMARY)
        self.y += 28

    def notes(self):
        notes = self.c.get("notes")
        if not notes:
            return
        for line in pdf.wrap(notes, "F1", 9, CONTENT_W - 24):
            self.ensure_space(12)
            self.text(LEFT + 12, self.y, line)
            self.y += 12
        self.y += 12

    def payment(self):
        co = self.company
        box_h = 68
        self.ensure_space(box_h)
        y = self.y
        self.doc.rect(LEFT, y, CONTENT_W, box_h, fill=LIGHT_BG, stroke=BORDER, radius=3)
        py = y + 10
        self.text(LEFT + 12, py, "Payment details", "F2", 9, PRIMARY)
        py += 16
        self.text(LEFT + 12, py, f"IBAN: {co['iban']}")
        self.text(LEFT + 200, py, f"Bank: {co['bank']}")
        py += 14
        self.text(LEFT + 12, py, f"Reference: {self.number}")
        if co.get("bic"):
            self.text(LEFT + 200, py, f"BIC: {co['bic']}")
        py += 14
        self.text(LEFT + 12, py, f"Account holder: {co['name']}")
        self.y += box_h


def render_invoice(content: dict, invoice_number: str | None, *, draft: bool) -> bytes:
    """The invoice PDF for frozen `content`. A draft shows "DRAFT" instead of a
    number and carries a diagonal DRAFT mark on every page."""
    if not draft and not invoice_number:
        raise ValueError("a final invoice needs its invoice number")
    layout = _Layout(content, "DRAFT" if draft else invoice_number, draft)
    layout.header()
    layout.parties_from()
    layout.parties_to()
    layout.items()
    layout.totals()
    layout.notes()
    layout.payment()
    layout._finish_page()
    return layout.doc.to_bytes()
