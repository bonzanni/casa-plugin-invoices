"""Finalize under a recoverable claim, resume after a crash, and the package (spec 5.3-5.5).

The caller holds `store.write_lock()` for `finalize` (the dispatcher does, for
every writing tool); nothing here takes it, since it is not reentrant.

A finalize writes, in order and each durable before the next:
`finalizing/<n>.json` (the claim: number, source draft, frozen content, the
rendered email), `invoices/<n>.pdf`, `invoices/<n>.json`, then deletes the
draft and the claim. Every finalize first completes any claim it finds, so a
crash at any point ends with the same number and the same frozen content, and
no new number is assigned while a claim is open.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
from typing import Callable

import casa_handoff
import email_template
import invoice
import invoice_pdf
import model
import money
import numbering
import store
from model import ValidationError

PRODUCER = "invoices"

#: Test hook, called with the step name after each step of `complete`
#: ("finalizing" after the claim is written).
_STEP_HOOK: Callable[[str], None] | None = None


class RefMismatch(Exception):
    """The draft, company or customer changed since the summary was approved."""


class PublishError(Exception):
    """The invoice is complete but its handoff copy could not be published."""


def _step(name: str) -> None:
    if _STEP_HOOK is not None:
        _STEP_HOOK(name)


def _records(sub: str) -> list[dict]:
    """Every `<sub>/*.json` record (temp files start with a dot and are skipped)."""
    out = []
    for name in store.listdir(sub):
        if name.endswith(".json") and not name.startswith("."):
            rec = store.read_json(f"{sub}/{name}")
            if not isinstance(rec, dict):
                raise store.StoreError(f"{sub}/{name}: not an invoice record")
            out.append(rec)
    return out


def _numbers(sub: str) -> list[str]:
    return [n[:-5] for n in store.listdir(sub)
            if n.endswith(".json") and not n.startswith(".")]


def _check_number(number) -> str:
    if (not isinstance(number, str) or not number or "/" in number
            or number.startswith(".")):
        raise ValidationError(f"not an invoice number: {number!r}")
    return number


def _read_pdf(rel: str) -> bytes | None:
    try:
        with open(store.path(rel), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- claim & resume

def complete(record: dict) -> None:
    """Carry a claim through to an issued invoice. Idempotent: every step
    already done is kept; the PDF is (re-)rendered from the frozen content."""
    n = record["invoice_number"]
    existing = store.read_json(f"invoices/{n}.json")
    if existing is None:
        # Always render from the frozen content (deterministic), never trust
        # bytes already on disk: the PDF must be exactly the approved content.
        data = invoice_pdf.render_invoice(record["content"], n, draft=False)
        store.write_bytes(f"invoices/{n}.pdf", data)
        _step("pdf")
        store.write_json(f"invoices/{n}.json",
                         {**record, "pdf_sha256": hashlib.sha256(data).hexdigest()})
        _step("invoice")
    elif (existing.get("source_draft") != record["source_draft"]
          or existing.get("content") != record["content"]):
        # Never drop an approved claim silently over another invoice.
        raise store.StoreError(
            f"invoices/{n}.json is a different invoice than the open claim "
            f"finalizing/{n}.json for draft {record['source_draft']}; resolve by hand")
    store.delete(f"drafts/{record['source_draft']}.json")
    _step("draft_deleted")
    store.delete(f"finalizing/{n}.json")
    _step("done")


def resume_pending() -> list[str]:
    """Complete every open claim; returns the numbers completed."""
    done = []
    for rec in _records("finalizing"):
        complete(rec)
        done.append(rec["invoice_number"])
    return done


def is_draft_locked(draft_id: str) -> bool:
    """True while a claim names this draft (edit and discard refuse)."""
    return any(r.get("source_draft") == draft_id for r in _records("finalizing"))


def used_draft_ids() -> set[str]:
    """Draft ids named by an issued invoice or an open claim. A new draft must
    not reuse one: finalize treats the id as already finalized."""
    return {r["source_draft"] for r in _records("invoices") + _records("finalizing")
            if r.get("source_draft")}


# ---------------------------------------------------------------- finalize

def _email(content: dict, number: str) -> dict:
    """The email from the approved (frozen) template, never a reloaded one."""
    subject, body = email_template.render(content["email_template"],
                                          email_template.email_values(content, number))
    rcpt = content["customer"].get("email") or {}
    return {
        "to": ", ".join(rcpt.get("to") or []),
        "cc": ", ".join(rcpt.get("cc") or []),
        "bcc": ", ".join(rcpt.get("bcc") or []),
        "from_address": content["company"].get("email") or "",
        "subject": subject,
        "body": body,
    }


def finalize(draft_id: str, approval_ref: str, plugin_root: str) -> dict:
    """Issue the draft as the next invoice and return its package (spec 5.5).

    Idempotent per draft: a draft already issued or claimed returns that
    invoice's package and never takes a second number."""
    resume_pending()

    for rec in _records("invoices") + _records("finalizing"):
        if rec.get("source_draft") == draft_id:
            if invoice.compute_ref(rec.get("content")) != approval_ref:
                # The id was drawn again for a new draft: never hand back the
                # old invoice as if it were the newly approved one.
                raise RefMismatch(
                    f"draft {draft_id} was already issued as {rec['invoice_number']} "
                    "with different content; create a new draft")
            return package(rec["invoice_number"])

    draft = store.read_json(f"drafts/{draft_id}.json")
    if draft is None:
        raise ValidationError(f"no draft {draft_id}")
    company = store.read_json("company.json")
    if company is None:
        raise ValidationError("Set up the company first (invoice_company_set)")
    customer = store.read_json(f"customers/{draft['customer_id']}.json")
    if customer is None:
        raise ValidationError(f"customer {draft['customer_id']} no longer exists")
    content = invoice.frozen_content(draft, company, customer,
                                     email_template.load(customer, plugin_root))
    if invoice.compute_ref(content) != approval_ref:
        raise RefMismatch(
            f"draft {draft_id} (or the company or customer details or email template "
            "on it) changed "
            "since the summary was approved; show the current summary again and "
            "get approval for its Ref before finalizing")

    year = model.parse_date(content["invoice_date"], "invoice_date").year
    number, _seq = numbering.next_number(
        company["numbering"]["pattern"], year,
        _numbers("invoices") + _numbers("finalizing"), company["numbering"])

    record = {
        "invoice_number": number,
        "source_draft": draft_id,
        "content": content,
        "email": _email(content, number),
        "finalized_at": datetime.datetime.now(datetime.timezone.utc)
                                         .isoformat(timespec="seconds"),
    }
    store.write_json(f"finalizing/{number}.json", record)
    _step("finalizing")
    complete(record)
    return package(number)


# ---------------------------------------------------------------- package

def package(number: str) -> dict:
    """Publish a fresh handoff copy of the stored PDF and return the package.

    The email is the one rendered at finalize, so later template or customer
    changes never alter a re-issued package."""
    n = _check_number(number)
    rec = store.read_json(f"invoices/{n}.json")
    if rec is None:
        raise ValidationError(f"no invoice {n}")
    if rec.get("legacy"):
        raise ValidationError(
            f"{n} was issued by lesina-invoice; the original PDF is in Gmail's "
            "Sent folder")
    data = _read_pdf(f"invoices/{n}.pdf")
    if data is None or hashlib.sha256(data).hexdigest() != rec.get("pdf_sha256"):
        raise store.StoreError(
            f"invoices/{n}.pdf is missing or does not match the issued invoice")
    try:
        pub = casa_handoff.publish(PRODUCER, f"{n}.pdf", data=data)
    except casa_handoff.HandoffError as exc:
        raise PublishError(
            f"{n}: the invoice is issued; call invoice_package to publish it "
            f"again (the handoff failed: {exc})") from exc
    content = rec["content"]
    return {
        "invoice_number": n,
        "pdf_path": pub["path"],
        "email": copy.deepcopy(rec["email"]),
        "request_id": f"invoice-{n}",
        "total": money.format_minor(content["totals"]["total"],
                                    content["company"]["currency"]),
    }
