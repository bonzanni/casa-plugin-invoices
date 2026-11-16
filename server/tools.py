# server/tools.py
"""The 14 MCP tools (spec 5.4): schemas and handlers, registered into
`invoices_server.TOOLS`. A handler returns text or a dict (sent as JSON) and
raises for errors; the dispatcher holds the write lock around every tool
registered with `writes=True`, so nothing here takes it.
"""
from __future__ import annotations

import os
import pathlib
import re
import secrets

import email_template
import finalize
import invoice
import invoice_pdf
import invoices_server
import model
import numbering
import store
from model import ValidationError

PLUGIN_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
OUTBOX_ENV = "CASA_PLUGIN_OUTBOX_DIR"
DEFAULT_OUTBOX = "/data/plugin-outbox"
SETUP_FIRST = "Set up the company first (invoice_company_set)"

invoices_server.USER_ERRORS[:] = [ValidationError, store.StoreError,
                                  finalize.RefMismatch, finalize.PublishError]


def tool(name: str, description: str, properties: dict | None = None,
         required: tuple = (), *, writes: bool = False, setup: bool = True):
    """Register the decorated handler. `setup=True`: refuse until company.json exists."""
    schema = {"type": "object", "properties": properties or {},
              "required": list(required), "additionalProperties": False}

    def wrap(fn):
        def run(args: dict):
            for key in required:
                if args.get(key) is None:
                    raise ValidationError(f"{key}: is required")
            if setup and not store.exists("company.json"):
                raise ValidationError(SETUP_FIRST)
            return fn(args)
        invoices_server.TOOLS[name] = {"description": description, "schema": schema,
                                       "fn": run, "writes": writes}
        return fn
    return wrap


# ---------------------------------------------------------------- helpers

def _key(value, what: str) -> str:
    """A file-name key (customer id, draft id, invoice number) or ValidationError."""
    if not isinstance(value, str) or not value or "/" in value or value.startswith("."):
        raise ValidationError(f"{what}: {value!r} is not valid")
    return value


def _company() -> dict:
    company = store.read_json("company.json")
    if company is None:
        raise ValidationError(SETUP_FIRST)
    return company


def _customer(cid) -> dict:
    customer = store.read_json(f"customers/{_key(cid, 'customer')}.json")
    if customer is None:
        raise ValidationError(f"no customer {cid} (see invoice_customer_list)")
    return customer


def _draft(draft_id) -> dict:
    draft = store.read_json(f"drafts/{_key(draft_id, 'draft_id')}.json")
    if draft is None:
        raise ValidationError(f"no draft {draft_id}")
    return draft


def _records(sub: str) -> list[dict]:
    """Every `<sub>/*.json` record; one deleted since the listing is skipped."""
    recs = (store.read_json(f"{sub}/{n}") for n in store.listdir(sub)
            if n.endswith(".json") and not n.startswith("."))
    return [r for r in recs if r is not None]


def _field(rec: dict, key: str):
    """A field of an invoice record: top level (legacy), else its frozen content."""
    if rec.get(key) is not None:
        return rec[key]
    content = rec.get("content")
    return content.get(key) if isinstance(content, dict) else None


def _issued_numbers() -> list[str]:
    return [n[:-5] for sub in ("invoices", "finalizing") for n in store.listdir(sub)
            if n.endswith(".json") and not n.startswith(".")]


def _content(draft: dict, company: dict, customer: dict) -> dict:
    return invoice.frozen_content(draft, company, customer,
                                  email_template.load(customer, PLUGIN_ROOT))


def _summary(draft: dict, company: dict, customer: dict) -> str:
    content = _content(draft, company, customer)
    return invoice.render_summary(draft, content, invoice.compute_ref(content))


def _money(minor: int, company: dict) -> str:
    return model.format_money(minor, company["currency"], company["locale"])


def _this_year(company: dict) -> int:
    return int(model.today(company["timezone"])[:4])


# ---------------------------------------------------------------- company

@tool("invoice_company_get", "Show the sender (company) profile used on every invoice.",
      setup=False)
def company_get(args):
    company = store.read_json("company.json")
    if company is None:
        return f"No company is set up yet. {SETUP_FIRST}."
    return company


def _year_of(pattern: str, number: str) -> tuple[int, int]:
    for year in sorted({int(y) for y in re.findall(r"(?=([0-9]{4}))", number)}):
        if 1000 <= year:
            seq = numbering.parse_seq(pattern, number, year)
            if seq is not None:
                return year, seq
    raise ValidationError(
        f"last_invoice_number: {number!r} does not match the number pattern {pattern!r}")


@tool("invoice_company_set",
      "Create or update the sender profile (partial update: only the fields given "
      "change; null clears an optional field). Fields: name, address_lines[], country, "
      "vat_id, vat_label, registration_id, registration_label, email, iban, bic, bank, "
      "payment_terms_days, default_vat_rate (\"21\"), tax_name, currency, locale "
      "(nl-NL, de-DE, en-GB, en-US), timezone, numbering {pattern (\"INV-{YYYY}-{seq:4}\"), "
      "next_seq, seq_year}. At first setup ask for the last invoice number already used "
      "in the series and pass it as last_invoice_number; numbering then continues after it. "
      "Changing numbering.pattern continues the current series at its next number unless "
      "next_seq is given too.",
      {"fields": {"type": "object", "description": "Company fields to set."},
       "last_invoice_number": {"type": "string",
                               "description": "Last number already issued, e.g. INV-2026-0041."}},
      ("fields",), writes=True, setup=False)
def company_set(args):
    fields = args["fields"]
    if not isinstance(fields, dict):
        raise ValidationError("fields: expected an object")
    current = store.read_json("company.json")
    num_patch = fields.get("numbering") or {}
    if not isinstance(num_patch, dict):
        raise ValidationError("numbering: expected an object")
    if "pattern" in num_patch:
        numbering.validate_pattern(num_patch["pattern"])
    company = model.merge_company(current, fields)
    n = company["numbering"]
    year = _this_year(company)
    old_pattern = (current or model.COMPANY_DEFAULTS)["numbering"]["pattern"]
    floor_year = (current or model.COMPANY_DEFAULTS)["numbering"]["seq_year"]
    if n["pattern"] != old_pattern and floor_year not in (None, year):
        # The floor is one (year, seq) pair; a pattern change would replace it.
        raise ValidationError(
            f"numbering.pattern: a starting number for {floor_year} is set; change the "
            f"pattern after issuing that year's invoices, or set last_invoice_number "
            f"for the current year first")
    if "next_seq" in num_patch and "seq_year" not in num_patch:
        n["seq_year"] = year                # a bare next_seq is for this year
    if n["pattern"] != old_pattern and "next_seq" not in num_patch:
        # Ruling R5: carry the series forward from the old pattern's next number.
        _number, seq = numbering.next_number(old_pattern, year, _issued_numbers(),
                                             (current or model.COMPANY_DEFAULTS)["numbering"])
        n["next_seq"], n["seq_year"] = seq, year
    last = args.get("last_invoice_number")
    if last is not None:
        if not isinstance(last, str):
            raise ValidationError("last_invoice_number: must be text")
        n["seq_year"], seq = _year_of(n["pattern"], last.strip())
        n["next_seq"] = seq + 1
    company = model.validate_company(company)
    store.write_json("company.json", company)
    next_number, _ = numbering.next_number(n["pattern"], year, _issued_numbers(), n)
    return {"company": company, "next_invoice_number": next_number}


# ---------------------------------------------------------------- customers

CUSTOMER_ARG = {"customer": {"type": "string", "description": "Customer id, e.g. acme."}}


@tool("invoice_customer_list", "List the customers (id, name, email recipients).", setup=False)
def customer_list(args):
    return {"customers": [{"id": c["id"], "name": c["name"], "email": c.get("email")}
                          for c in _records("customers")]}


@tool("invoice_customer_get", "Show one customer.", CUSTOMER_ARG, ("customer",), setup=False)
def customer_get(args):
    return _customer(args["customer"])


@tool("invoice_customer_set",
      "Create or update a customer (partial update; null clears an optional field). "
      "Fields: name, address_lines[], country, vat_id, registration_id, "
      "email {to[], cc[], bcc[]}, payment_terms_days and vat_rate (override the "
      "company's), vat_note (e.g. \"VAT reverse-charged\" at rate 0), email_template "
      "(\"Subject: ...\" line, blank line, body).",
      {**CUSTOMER_ARG, "fields": {"type": "object", "description": "Customer fields to set."}},
      ("customer", "fields"), writes=True, setup=False)
def customer_set(args):
    cid, fields = _key(args["customer"], "customer"), args["fields"]
    if not isinstance(fields, dict):
        raise ValidationError("fields: expected an object")
    if fields.get("id") not in (None, cid):
        raise ValidationError(f"customer id: {fields['id']!r} differs from customer {cid!r}")
    current = store.read_json(f"customers/{cid}.json")
    customer = model.merge_customer(current, {**fields, "id": cid})
    if customer.get("email_template"):
        email_template.validate(customer["email_template"])
    store.write_json(f"customers/{cid}.json", customer)
    return customer


@tool("invoice_customer_remove",
      "Remove a customer that has no open drafts (issued invoices keep their own copy "
      "of the customer's details).",
      CUSTOMER_ARG, ("customer",), writes=True, setup=False)
def customer_remove(args):
    cid = _customer(args["customer"])["id"]
    drafts = sorted(d["draft_id"] for d in _records("drafts") if d.get("customer_id") == cid)
    if drafts:
        raise ValidationError(
            f"customer {cid} has open drafts ({', '.join(drafts)}); finalize or discard them first")
    store.delete(f"customers/{cid}.json")
    return f"Removed customer {cid}."


# ---------------------------------------------------------------- drafts

ITEM_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "description": {"type": "string"},
                   "detail": {"type": "string"}, "qty": {"type": "string"},
                   "unit_price": {"type": "string",
                                  "description": "Plain amount with a dot: \"13000.00\"."}},
    "required": ["description", "qty", "unit_price"],
}


def _source(cid: str, choice) -> dict | None:
    mine = [r for r in _records("invoices") if _field(r, "customer_id") == cid]
    if choice is None:
        choice = "last" if mine else "none"
    if choice == "none":
        return None
    if choice == "last":
        if not mine:
            raise ValidationError(
                f"customer {cid} has no previous invoice; give items (from: \"none\")")
        return max(mine, key=lambda r: (_field(r, "invoice_date") or "", r["invoice_number"]))
    rec = store.read_json(f"invoices/{_key(choice, 'from')}.json")
    if rec is None:
        raise ValidationError(f"from: no invoice {choice}")
    return rec                          # new_draft refuses another customer's invoice


@tool("invoice_new",
      "Start a draft for a customer and return its summary (show it verbatim). "
      "from: \"last\" (default when the customer has a previous invoice: copies its lines "
      "and moves the period one month on), an invoice number to copy, or \"none\" (then "
      "items are required). items replace the copied lines. period: \"2026-11\" or "
      "\"November 2026\". invoice_date: YYYY-MM-DD, default today.",
      {**CUSTOMER_ARG,
       "from": {"type": "string", "description": "\"last\", an invoice number, or \"none\"."},
       "period": {"type": "string"}, "invoice_date": {"type": "string"},
       "items": {"type": "array", "items": ITEM_SCHEMA}},
      ("customer",), writes=True)
def invoice_new(args):
    company, customer = _company(), _customer(args["customer"])
    source = _source(customer["id"], args.get("from"))
    existing = {n[:-5] for n in store.listdir("drafts") if n.endswith(".json")}
    draft = invoice.new_draft(customer, company, source, period=args.get("period"),
                              invoice_date=args.get("invoice_date"),
                              items=args.get("items"),
                              existing_ids=frozenset(existing | finalize.used_draft_ids()))
    store.write_json(f"drafts/{draft['draft_id']}.json", draft)
    return _summary(draft, company, customer)


@tool("invoice_edit",
      "Apply operations to a draft, all or none, and return the new summary (show it "
      "verbatim). operations is a list of flat objects: "
      "{\"op\": \"change_item\", \"id\": \"L1\", \"field\": \"unit_price\"|\"qty\"|"
      "\"description\"|\"detail\", \"value\": ...}; {\"op\": \"remove_item\", \"id\": \"L2\"}; "
      "{\"op\": \"add_item\", \"item\": {\"description\", \"detail\"?, \"qty\", \"unit_price\"}}; "
      "{\"op\": \"set\", \"field\": \"invoice_date\"|\"period\"|\"notes\"|\"vat_rate\", "
      "\"value\": ...} (null clears period or notes).",
      {"draft_id": {"type": "string"},
       "operations": {"type": "array", "items": {
           "type": "object",
           "properties": {"op": {"type": "string",
                                 "enum": ["change_item", "remove_item", "add_item", "set"]},
                          "id": {"type": "string"}, "field": {"type": "string"},
                          "value": {}, "item": ITEM_SCHEMA},
           "required": ["op"]}}},
      ("draft_id", "operations"), writes=True)
def invoice_edit(args):
    draft_id = _key(args["draft_id"], "draft_id")
    if finalize.is_draft_locked(draft_id):
        raise ValidationError(f"draft {draft_id} is being finalized and can no longer change")
    company, draft = _company(), _draft(draft_id)
    customer = _customer(draft["customer_id"])
    draft = invoice.apply_edits(draft, args["operations"], company, customer)
    store.write_json(f"drafts/{draft_id}.json", draft)
    return _summary(draft, company, customer)


def _invoice_view(rec: dict, company: dict) -> dict:
    view = {k: rec.get(k) for k in ("invoice_number", "legacy", "source_draft",
                                    "finalized_at") if rec.get(k) is not None}
    for k in ("customer_id", "invoice_date", "due_date", "period", "line_items",
              "vat_rate", "notes", "source_invoice"):
        if _field(rec, k) is not None:
            view[k] = _field(rec, k)
    content = rec.get("content")
    if isinstance(content, dict):
        co = content["company"]
        view["customer_name"] = content["customer"]["name"]
        view["totals"] = {k: model.format_money(content["totals"][k], co["currency"],
                                                co["locale"])
                          for k in ("subtotal", "vat", "total")}
    if rec.get("email"):
        view["email"] = rec["email"]
    return view


@tool("invoice_get",
      "Show a draft (by draft id: its summary) or an invoice (by invoice number).",
      {"id": {"type": "string", "description": "Draft id or invoice number."}}, ("id",))
def invoice_get(args):
    key = _key(args["id"], "id")
    company = _company()
    draft = store.read_json(f"drafts/{key}.json")
    if draft is not None:
        return _summary(draft, company, _customer(draft["customer_id"]))
    rec = store.read_json(f"invoices/{key}.json")
    if rec is None:
        raise ValidationError(f"no draft or invoice {key}")
    return _invoice_view(rec, company)


@tool("invoice_list",
      "List open drafts and issued invoices, newest first, optionally for one customer "
      "and/or one year (of the invoice date).",
      {**CUSTOMER_ARG, "year": {"type": "integer"}})
def invoice_list(args):
    company = _company()
    cid, year = args.get("customer"), args.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            raise ValidationError(f"year: {year!r} is not a year") from None

    def keep(rec):
        date = _field(rec, "invoice_date") or ""
        return ((cid is None or _field(rec, "customer_id") == cid)
                and (year is None or date[:4] == str(year)))

    def newest(rows, key):
        return sorted(rows, key=lambda r: (r.get("invoice_date") or "", r[key]), reverse=True)

    drafts = []
    for d in filter(keep, _records("drafts")):
        t = invoice.totals(d["line_items"], d["vat_rate"], company["currency"])
        drafts.append({"draft_id": d["draft_id"], "customer_id": d["customer_id"],
                       "invoice_date": d["invoice_date"], "period": d.get("period"),
                       "total": _money(t["total"], company)})
    invoices = []
    for r in filter(keep, _records("invoices")):
        row = {"invoice_number": r["invoice_number"],
               "customer_id": _field(r, "customer_id"),
               "invoice_date": _field(r, "invoice_date"), "period": _field(r, "period")}
        if r.get("legacy"):
            row["legacy"] = True
        else:
            co = r["content"]["company"]
            row["total"] = model.format_money(r["content"]["totals"]["total"],
                                              co["currency"], co["locale"])
        invoices.append(row)
    return {"drafts": newest(drafts, "draft_id"), "invoices": newest(invoices, "invoice_number")}


def _write_outbox(name: str, data: bytes) -> str:
    outbox = os.environ.get(OUTBOX_ENV) or DEFAULT_OUTBOX
    if not os.path.isdir(outbox):
        raise ValidationError(
            f"the media outbox {outbox} does not exist; this needs a Casa version "
            "with plugin media")
    final, part = os.path.join(outbox, name), os.path.join(outbox, name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(part, final)
    except BaseException:
        if os.path.exists(part):
            os.unlink(part)
        raise
    dfd = os.open(outbox, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return final


@tool("invoice_preview",
      "Render the draft as a PDF marked DRAFT into Casa's media outbox and return its "
      "path, for send_media in the chat.",
      {"draft_id": {"type": "string"}}, ("draft_id",))
def invoice_preview(args):
    company, draft = _company(), _draft(args["draft_id"])
    content = _content(draft, company, _customer(draft["customer_id"]))
    data = invoice_pdf.render_invoice(content, None, draft=True)
    name = f"invoice-draft-{draft['draft_id']}-{secrets.token_hex(3)}.pdf"
    return f"Path: {_write_outbox(name, data)}"


def _check_finalize_customer(draft_id: str, given) -> None:
    """The `customer` shown on the approval card must be the draft's (id or
    name, trimmed, any case); for an issued draft, the issued invoice's."""
    draft = store.read_json(f"drafts/{draft_id}.json")
    if draft is not None:
        cid = draft["customer_id"]
        name = (store.read_json(f"customers/{cid}.json") or {}).get("name") or cid
    else:
        found = [r["content"] for r in _records("invoices") + _records("finalizing")
                 if r.get("source_draft") == draft_id and isinstance(r.get("content"), dict)]
        if not found:
            return                      # finalize reports the missing draft
        cid, name = found[0]["customer_id"], found[0]["customer"]["name"]
    norm = given.strip().casefold() if isinstance(given, str) else None
    if norm not in (cid.casefold(), name.strip().casefold()):
        raise ValidationError(
            f"customer: {given!r} is not the customer of draft {draft_id}; it is for "
            f"{name} ({cid}). Nothing was issued.")


@tool("invoice_finalize",
      "Issue the draft as the next numbered invoice, only after the operator explicitly "
      "approved its summary: approval_ref is the Ref line of that summary. Renders and "
      "stores the PDF, publishes a copy to handoff, deletes the draft and returns the "
      "package {invoice_number, pdf_path, email, request_id, total}; hand it back unchanged.",
      {"draft_id": {"type": "string"},
       "customer": {"type": "string", "description": "Customer display name, for the approval."},
       "approval_ref": {"type": "string", "description": "The Ref of the approved summary."}},
      ("draft_id", "customer", "approval_ref"), writes=True)
def invoice_finalize(args):
    draft_id = _key(args["draft_id"], "draft_id")
    _check_finalize_customer(draft_id, args["customer"])
    try:
        return finalize.finalize(draft_id, args["approval_ref"], PLUGIN_ROOT)
    except finalize.RefMismatch as exc:
        draft = store.read_json(f"drafts/{draft_id}.json")
        if draft is None:
            raise
        current = _summary(draft, _company(), _customer(draft["customer_id"]))
        raise finalize.RefMismatch(
            f"{exc}. Nothing was issued. Show this summary to the operator and finalize "
            f"only after approval of its Ref:\n\n{current}") from None


@tool("invoice_package",
      "For an issued invoice: publish its stored PDF to handoff again (handoff copies "
      "expire after 7 days) and return the package again.",
      {"invoice_number": {"type": "string"}}, ("invoice_number",))
def invoice_package(args):
    return finalize.package(args["invoice_number"])


@tool("invoice_discard", "Delete a draft.", {"draft_id": {"type": "string"}},
      ("draft_id",), writes=True)
def invoice_discard(args):
    draft_id = _key(args["draft_id"], "draft_id")
    if finalize.is_draft_locked(draft_id):
        raise ValidationError(f"draft {draft_id} is being finalized and cannot be discarded")
    _draft(draft_id)
    store.delete(f"drafts/{draft_id}.json")
    return f"Discarded draft {draft_id}."
