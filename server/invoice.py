"""Invoice core: exact totals, new drafts, edits, the frozen content and its
Ref, and the summary text the operator approves.

Money is integer minor units throughout; no float ever touches an amount.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets

import model
import money
from model import ValidationError

DRAFT_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
DRAFT_ID_LEN = 4
ITEM_FIELDS = ("unit_price", "qty", "description", "detail")
SET_FIELDS = ("invoice_date", "period", "notes", "vat_rate")
LABEL_WIDTH = 22
AMOUNT_WIDTH = 14

_OP_KEYS = {
    "change_item": {"op", "id", "field", "value"},
    "remove_item": {"op", "id"},
    "add_item": {"op", "item"},
    "set": {"op", "field", "value"},
}


# ---------------------------------------------------------------- totals

def _div_half_up(num: int, den: int) -> int:
    """num / den rounded half away from zero (den > 0), integers only."""
    q = (abs(num) * 2 + den) // (den * 2)
    return -q if num < 0 else q


def _micro(qty: str) -> int:
    """A canonical qty string ("1.5") as integer millionths."""
    whole, _, frac = qty.partition(".")
    return int(whole) * 10**6 + int(frac.ljust(6, "0") or "0")


def line_total_minor(qty: str, unit_price_minor: int) -> int:
    return _div_half_up(_micro(qty) * unit_price_minor, 10**6)


def totals(items: list[dict], vat_rate: str, currency: str) -> dict:
    lines = [line_total_minor(i["qty"], money.to_minor(i["unit_price"], currency))
             for i in items]
    subtotal = sum(lines)
    vat = _div_half_up(subtotal * model.rate_bp(vat_rate), 10000)
    return {"lines": lines, "subtotal": subtotal, "vat": vat, "total": subtotal + vat}


def _check_not_negative(draft: dict, currency: str) -> None:
    if totals(draft["line_items"], draft["vat_rate"], currency)["total"] < 0:
        raise ValidationError(
            "the invoice total would be below zero; credit notes are not supported")


# ---------------------------------------------------------------- new draft

def _draw_id() -> str:
    return "".join(secrets.choice(DRAFT_ID_ALPHABET) for _ in range(DRAFT_ID_LEN))


def _date(value, field: str) -> str:
    return model.parse_date(value, field).isoformat()


def _terms(company: dict, customer: dict) -> int:
    days = customer.get("payment_terms_days")
    return company["payment_terms_days"] if days is None else days


def _normalize_items(items, currency: str) -> list[dict]:
    if not isinstance(items, list):
        raise ValidationError("items: expected a list of line items")
    out: list[dict] = []
    for item in items:
        out.append(model.normalize_item(item, {i["id"] for i in out}, currency))
    return out


def _source_field(source: dict, field: str):
    """A field of a previous invoice: top level, else its frozen `content`."""
    if source.get(field) is not None:
        return source[field]
    content = source.get("content")
    return content.get(field) if isinstance(content, dict) else None


def _same_customer(customer: dict, customer_id, what: str) -> None:
    if customer_id is not None and customer_id != customer["id"]:
        raise ValidationError(
            f"{what} is for customer {customer_id}, not {customer['id']}")


def new_draft(customer: dict, company: dict, source: dict | None, *,
              period: str | None, invoice_date: str | None, items: list | None,
              existing_ids: frozenset[str] = frozenset()) -> dict:
    """A new draft for `customer`, from scratch (`items`) or copied from `source`.

    With both, `items` are the lines and `source` still gives the period and
    `source_invoice`. `existing_ids` are the draft ids already in use.
    """
    currency = company["currency"]
    if source is not None:
        _same_customer(customer, _source_field(source, "customer_id"),
                       f"invoice {source.get('invoice_number')}")
    if items is not None:
        line_items = _normalize_items(items, currency)
    elif source is not None:
        line_items = _normalize_items(copy.deepcopy(_source_field(source, "line_items") or []),
                                      currency)
    else:
        line_items = []
    if not line_items:
        raise ValidationError(
            "items: a new invoice needs at least one line item "
            "(or copy one from a previous invoice)")

    if period is not None:
        period = model.parse_period(period)
    elif source is not None and _source_field(source, "period"):
        period = model.next_period(_source_field(source, "period"))

    if invoice_date is None:
        invoice_date = model.today(company["timezone"])
    invoice_date = _date(invoice_date, "invoice_date")

    vat_rate = customer.get("vat_rate")
    vat_rate = model.canonical_rate(company["default_vat_rate"] if vat_rate is None else vat_rate)

    draft_id = _draw_id()
    while draft_id in existing_ids:
        draft_id = _draw_id()

    draft = {
        "draft_id": draft_id,
        "customer_id": customer["id"],
        "invoice_date": invoice_date,
        "due_date": model.add_days(invoice_date, _terms(company, customer)),
        "period": period,
        "line_items": line_items,
        "vat_rate": vat_rate,
        "notes": None,
        "source_invoice": source.get("invoice_number") if source is not None else None,
    }
    _check_not_negative(draft, currency)
    return draft


# ---------------------------------------------------------------- edits

def _find(items: list[dict], iid) -> int:
    for n, item in enumerate(items):
        if item["id"] == iid:
            return n
    known = ", ".join(i["id"] for i in items)
    raise ValidationError(f"line item {iid}: no such item (the draft has {known})")


def _apply_one(d: dict, op, company: dict, customer: dict) -> None:
    currency = company["currency"]
    kind = op.get("op") if isinstance(op, dict) else None
    if not isinstance(kind, str) or kind not in _OP_KEYS:
        name = kind if isinstance(op, dict) else op
        raise ValidationError(
            f"operation {name!r}: use change_item, remove_item, add_item or set")
    unknown = sorted(set(op) - _OP_KEYS[kind])
    missing = sorted(_OP_KEYS[kind] - set(op))
    if unknown or missing:
        need = ", ".join(sorted(_OP_KEYS[kind] - {"op"}))
        raise ValidationError(
            f"{kind}: takes {need}"
            + (f"; unknown {', '.join(unknown)}" if unknown else "")
            + (f"; missing {', '.join(missing)}" if missing else ""))
    items = d["line_items"]

    if kind == "change_item":
        if op["field"] not in ITEM_FIELDS:
            raise ValidationError(
                f"change_item field {op['field']!r}: use {', '.join(ITEM_FIELDS)}")
        n = _find(items, op["id"])
        changed = {**items[n], op["field"]: op["value"]}
        others = {i["id"] for i in items} - {op["id"]}
        items[n] = model.normalize_item(changed, others, currency)
    elif kind == "remove_item":
        n = _find(items, op["id"])
        if len(items) == 1:
            raise ValidationError(
                f"line item {op['id']}: cannot remove the last line item "
                "(discard the draft instead)")
        del items[n]
    elif kind == "add_item":
        items.append(model.normalize_item(op["item"], {i["id"] for i in items}, currency))
    else:
        field, value = op["field"], op["value"]
        if field == "invoice_date":
            d["invoice_date"] = _date(value, "invoice_date")
            d["due_date"] = model.add_days(d["invoice_date"], _terms(company, customer))
        elif field == "period":
            d["period"] = None if value is None else model.parse_period(value)
        elif field == "notes":
            if value is None:
                d["notes"] = None
            elif not isinstance(value, str):
                raise ValidationError("notes: must be text")
            else:
                d["notes"] = model.check_text(value.strip(), "notes") or None
        elif field == "vat_rate":
            d["vat_rate"] = model.canonical_rate(value)
        else:
            raise ValidationError(f"set field {field!r}: use {', '.join(SET_FIELDS)}")


def apply_edits(draft: dict, ops: list[dict], company: dict, customer: dict) -> dict:
    """The draft with every operation applied, or ValidationError and no change."""
    if not isinstance(ops, list) or not ops:
        raise ValidationError("operations: expected a non-empty list")
    d = copy.deepcopy(draft)
    for op in ops:
        _apply_one(d, op, company, customer)
    _check_not_negative(d, company["currency"])
    return d


# ---------------------------------------------------------------- frozen content & Ref

def frozen_content(draft: dict, company: dict, customer: dict, template: str) -> dict:
    """Everything that goes on the PDF, plus the resolved email template
    (`email_template.load`) the email is rendered from; the Ref is its hash."""
    _same_customer(customer, draft["customer_id"], f"draft {draft.get('draft_id')}")
    content = {
        "company": {k: v for k, v in company.items() if k != "numbering"},
        "customer": {k: v for k, v in customer.items() if k != "email_template"},
        "customer_id": draft["customer_id"],
        "invoice_date": draft["invoice_date"],
        "due_date": draft["due_date"],
        "period": draft.get("period"),
        "line_items": draft["line_items"],
        "vat_rate": draft["vat_rate"],
        "notes": draft.get("notes"),
        "source_invoice": draft.get("source_invoice"),
        "email_template": template,
    }
    content = copy.deepcopy(content)
    content["totals"] = totals(content["line_items"], content["vat_rate"],
                               company["currency"])
    return content


def compute_ref(content: dict) -> str:
    canon = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- summary

def _qty_row(qty: str, unit_price: str, amount: str) -> str:
    """`    {qty} × {unit price}` with the line amount right-aligned."""
    left = f"    {qty} × {unit_price}"
    pad = max(1, LABEL_WIDTH + AMOUNT_WIDTH - len(left) - len(amount))
    return left + " " * pad + amount


def _row(label: str, amount: str) -> str:
    if len(label) > LABEL_WIDTH:
        label = label[:LABEL_WIDTH - 1] + "…"
    return f"{label:<{LABEL_WIDTH}}{amount:>{AMOUNT_WIDTH}}"


def render_summary(draft: dict, content: dict, ref: str) -> str:
    company = content["company"]
    currency, locale = company["currency"], company["locale"]
    t = content["totals"]

    def fmt_money(minor: int) -> str:
        return model.format_money(minor, currency, locale)

    title = f"**Draft {draft['draft_id']} — {content['customer']['name']}"
    if content.get("period"):
        title += f" — {model.period_label(content['period'])}"
    days = (model.parse_date(content["due_date"])
            - model.parse_date(content["invoice_date"])).days
    out = [
        title + "**",
        f"Invoice date: {model.format_date(content['invoice_date'], locale)}"
        f" · Due: {model.format_date(content['due_date'], locale)}"
        f" ({days} day{'' if days == 1 else 's'})",
    ]
    if content.get("source_invoice"):
        out.append(f"Copied from: {content['source_invoice']}")
    if content.get("notes"):
        out.append(f"Notes: {content['notes']}")
    out.append("```")
    for item, minor in zip(content["line_items"], t["lines"]):
        out.append(f"{item['id']}  {item['description']}")
        if item.get("detail"):
            out.append(f"    {item['detail']}")
        qty = item["qty"].replace(".", model.decimal_sep(locale))
        unit = fmt_money(money.to_minor(item["unit_price"], currency))
        out.append(_qty_row(qty, unit, fmt_money(minor)))
    out.append("-" * (LABEL_WIDTH + AMOUNT_WIDTH))
    out.append(_row("Subtotal", fmt_money(t["subtotal"])))
    rate = model.canonical_rate(content["vat_rate"])
    out.append(_row(f"{company['tax_name']} {rate}%", fmt_money(t["vat"])))
    out.append("```")
    out.append(f"**Total: {fmt_money(t['total'])}**")
    out.append(f"Draft: {draft['draft_id']}")
    out.append(f"Ref: {ref}")
    return "\n".join(out)
