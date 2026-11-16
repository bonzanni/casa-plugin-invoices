"""One-time import of the predecessor plugin's data, lesina-invoice (spec 5.7).

Reads `defaults.json` (company and the one customer) and `drafts/YYYY-MM.json`
from the old plugin's data dir. Every legacy draft is first given the state
the old plugin gives it on read (`normalizeDraft`, invoice.ts:140): its
`submit_state`, else `submitted` / `idle` from the boolean that drafts older
than v1.7.0 carry alone. Submitted drafts become `legacy: true` invoice
records under their original numbers; idle ones are reported and skipped; a
`pending` one (an email may have gone out) refuses the whole import.

Everything is read and validated before the first write, so a refusal leaves
the data dir untouched. The caller holds `store.write_lock()` (not reentrant).
"""
from __future__ import annotations

import decimal
import json
import os
import re

import model
import numbering
import store
from model import ValidationError

FALLBACK_CUSTOMER_ID = "customer"
PATTERN = "INV-{YYYY}-{seq:4}"
LEGACY_DIR_NAME = "lesina-invoice-inline"
_DRAFT_FILE = re.compile(r"([0-9]{4}-[0-9]{2})\.json\Z")
_ITEM_KEYS = ("id", "description", "detail", "qty", "unit_price")

STEP_0 = (
    "In lesina-invoice, re-save one default line's price at its current amount; "
    "it then writes defaults.json with all its values. Then run the import again.")
RUNBOOK = (
    "Cutover, to finish before lesina-invoice would issue its next invoice: "
    "(2) keep the n8n invoice workflows disabled, so lesina-invoice can no "
    "longer issue a number; (3) this import is done, and new invoices continue "
    "the series here; (4) Uninstall lesina-invoice (its data may be kept or "
    "erased).")


def default_legacy_dir() -> str:
    """The old plugin's data dir next to ours: `<plugins data>/lesina-invoice-inline`."""
    root = os.path.normpath(store.data_root())
    return os.path.join(os.path.dirname(root), LEGACY_DIR_NAME)


# ---------------------------------------------------------------- reading

def _load(path: str, what: str):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError(f"lesina-invoice {what}: unreadable JSON ({exc})") from None


def _state(draft: dict) -> str:
    """The state the old plugin gives the draft on read (normalizeDraft)."""
    state = draft.get("submit_state")
    if not state:
        return "submitted" if draft.get("submitted") else "idle"
    return state


def _vat_rate(btw_rate) -> str:
    """`btw_rate` (a fraction, 0.21) as a canonical percent string ("21"),
    exactly: through the float's shortest decimal text, never float math."""
    if isinstance(btw_rate, bool) or not isinstance(btw_rate, (int, float)):
        raise ValidationError(f"lesina-invoice defaults.json btw_rate: {btw_rate!r} is not a number")
    pct = decimal.Decimal(str(btw_rate)) * 100
    try:
        return model.canonical_rate(format(pct, "f"))
    except ValidationError as e:
        raise ValidationError(f"lesina-invoice defaults.json btw_rate: {e}") from None


def _lines(party: dict) -> list:
    return [v for v in (party.get("address1"), party.get("address2")) if v]


def _optional(**fields) -> dict:
    return {k: v for k, v in fields.items() if v not in (None, "")}


def _company(defaults: dict) -> dict:
    s = defaults.get("sender")
    if not isinstance(s, dict):
        raise ValidationError("lesina-invoice defaults.json: no sender")
    patch = {
        "name": s.get("name"),
        "address_lines": _lines(s),
        "country": s.get("country"),
        "iban": s.get("iban"),
        "bank": s.get("bank"),
        **_optional(vat_id=s.get("vat"), registration_id=s.get("kvk"), email=s.get("email")),
        "vat_label": "BTW-id",
        "registration_label": "KvK",
        "tax_name": "BTW",
        "default_vat_rate": _vat_rate(defaults.get("btw_rate")),
        "payment_terms_days": defaults.get("payment_terms_days"),
        "locale": "nl-NL",
        "timezone": "Europe/Amsterdam",
    }
    try:
        return model.merge_company(None, patch)
    except ValidationError as e:
        raise ValidationError(f"lesina-invoice defaults.json sender: {e}") from None


def customer_id(name) -> str:
    """The customer id for the legacy recipient: the first word of its name,
    lowercase, letters and digits only ("Acme Biotech B.V." -> "acme")."""
    words = name.split() if isinstance(name, str) else []
    slug = re.sub(r"[^a-z0-9]", "", words[0].lower())[:40] if words else ""
    return slug or FALLBACK_CUSTOMER_ID


def _customer(defaults: dict) -> dict:
    r = defaults.get("recipient")
    if not isinstance(r, dict):
        raise ValidationError("lesina-invoice defaults.json: no recipient")
    patch = {
        "id": customer_id(r.get("name")),
        "name": r.get("name"),
        "address_lines": _lines(r),
        "country": r.get("country"),
        **_optional(vat_id=r.get("vat"), registration_id=r.get("kvk")),
        "email": {"to": [r["email_to"]] if r.get("email_to") else [],
                  "cc": [r["snelstart_inbox"]] if r.get("snelstart_inbox") else []},
    }
    try:
        return model.merge_customer(None, patch)
    except ValidationError as e:
        raise ValidationError(f"lesina-invoice defaults.json recipient: {e}") from None


def _item(raw, used: set, currency: str) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("line item: expected an object")
    item = {k: raw[k] for k in _ITEM_KEYS if k in raw}
    # Legacy ids use "_" (mgmt_fee), which item ids here do not allow; a
    # free id keeps its meaning as "mgmt-fee", anything else is renumbered.
    iid = item.get("id")
    iid = iid.replace("_", "-") if isinstance(iid, str) else None
    item["id"] = iid if iid and re.fullmatch(r"[A-Za-z0-9-]{1,20}", iid) and iid not in used else None
    qty = item.get("qty")
    if isinstance(qty, float):
        item["qty"] = format(decimal.Decimal(repr(qty)), "f")
    return model.normalize_item(item, used, currency)


def _invoice(period: str, draft: dict, currency: str, cid: str) -> dict:
    number = draft.get("invoice_number")
    if not isinstance(number, str) or "/" in number or number.startswith("."):
        raise ValidationError(f"invoice number {number!r} is not usable")
    year = int(period[:4])
    if numbering.parse_seq(PATTERN, number, year) is None:
        raise ValidationError(
            f"invoice number {number} does not match INV-{year}-NNNN for period {period}")
    items = draft.get("line_items")
    if not isinstance(items, list) or not items:
        raise ValidationError("no line items")
    line_items: list[dict] = []
    for raw in items:
        line_items.append(_item(raw, {i["id"] for i in line_items}, currency))
    return {
        "invoice_number": number,
        "legacy": True,
        "customer_id": cid,
        "period": period,
        "invoice_date": model.parse_date(draft.get("invoice_date"), "invoice_date").isoformat(),
        "due_date": model.parse_date(draft.get("due_date"), "due_date").isoformat(),
        "line_items": line_items,
    }


def _drafts(path: str) -> list[tuple[str, dict]]:
    ddir = os.path.join(path, "drafts")
    try:
        names = sorted(os.listdir(ddir))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        m = _DRAFT_FILE.fullmatch(name)
        if m is None:
            continue                     # the old plugin only writes YYYY-MM.json
        draft = _load(os.path.join(ddir, name), f"drafts/{name}")
        if not isinstance(draft, dict):
            raise ValidationError(f"lesina-invoice drafts/{name}: not a draft")
        out.append((m.group(1), draft))
    return out


# ---------------------------------------------------------------- import

def _existing(cid: str) -> list[str]:
    found = []
    if store.exists("company.json"):
        found.append("company.json")
    if store.exists(f"customers/{cid}.json"):
        found.append(f"customers/{cid}.json")
    for sub in ("invoices", "finalizing"):
        found += [f"{sub}/{n}" for n in store.listdir(sub)
                  if n.endswith(".json") and not n.startswith(".")]
    return found


def import_legacy(path: str | None, force: bool) -> dict:
    """Import lesina-invoice's company, customer and submitted invoices."""
    path = path or default_legacy_dir()
    if not os.path.isdir(path):
        raise ValidationError(f"no lesina-invoice data at {path}")

    defaults_path = os.path.join(path, "defaults.json")
    if not os.path.exists(defaults_path):
        raise ValidationError(f"lesina-invoice has no defaults.json at {path}. {STEP_0}")
    defaults = _load(defaults_path, "defaults.json")
    if not isinstance(defaults, dict):
        raise ValidationError("lesina-invoice defaults.json: not an object")
    company = _company(defaults)
    customer = _customer(defaults)
    cid = customer["id"]

    existing = _existing(cid)
    if existing and not force:
        raise ValidationError(
            f"invoices already holds data ({', '.join(existing[:3])}"
            f"{', …' if len(existing) > 3 else ''}); the import replaces the company "
            "and customer and adds the legacy invoices — run it with force to do so anyway")

    invoices, ignored, pending = [], [], []
    for period, draft in _drafts(path):
        state = _state(draft)
        if state == "pending":
            pending.append(period)
        elif state == "idle":
            ignored.append(period)
        elif state == "submitted":
            try:
                invoices.append(_invoice(period, draft, company["currency"], cid))
            except ValidationError as e:
                raise ValidationError(f"lesina-invoice drafts/{period}.json: {e}") from None
        else:
            raise ValidationError(
                f"lesina-invoice drafts/{period}.json: unknown submit_state {state!r}; "
                "resolve it in lesina-invoice first")
    if pending:
        raise ValidationError(
            f"lesina-invoice has a pending submission for {', '.join(pending)} (an email "
            "may have gone out); resolve it in lesina-invoice (check n8n) and run the "
            "import again")

    for rec in invoices:
        prior = store.read_json(f"invoices/{rec['invoice_number']}.json")
        if prior is not None and not prior.get("legacy"):
            raise ValidationError(
                f"{rec['invoice_number']} was already issued by this plugin; the import "
                "never replaces it")

    if invoices:
        year = max(int(r["period"][:4]) for r in invoices)
        seqs = [numbering.parse_seq(PATTERN, r["invoice_number"], year) for r in invoices]
        company["numbering"] = {"pattern": PATTERN,
                                "next_seq": max(s for s in seqs if s is not None) + 1,
                                "seq_year": year}
    else:
        year = int(model.today(company["timezone"])[:4])
    floor = store.read_json("company.json")
    floor = floor.get("numbering") if isinstance(floor, dict) else None
    if (isinstance(floor, dict) and floor.get("seq_year") == year
            and isinstance(floor.get("next_seq"), int)):
        # A forced re-import never lowers a floor raised by hand for that year.
        n = company["numbering"]
        n["next_seq"], n["seq_year"] = max(n["next_seq"], floor["next_seq"]), year
    company = model.validate_company(company)
    issued = [n[:-5] for n in store.listdir("invoices")
              if n.endswith(".json") and not n.startswith(".")]
    next_number, _seq = numbering.next_number(
        PATTERN, year, issued + [r["invoice_number"] for r in invoices], company["numbering"])

    # Company last: until it exists the plugin counts as not set up, so an
    # interrupted import is visible and is finished by running it again with force.
    for rec in invoices:
        store.write_json(f"invoices/{rec['invoice_number']}.json", rec)
    store.write_json(f"customers/{cid}.json", customer)
    store.write_json("company.json", company)
    return {
        "company": "imported",
        "customer": cid,
        "invoices": [r["invoice_number"] for r in invoices],
        "ignored_drafts": ignored,
        "next_number": next_number,
        "runbook": RUNBOOK,
    }
