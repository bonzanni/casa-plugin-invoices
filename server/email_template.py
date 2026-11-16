"""Plain-text invoice email: template lookup, validation and single-pass render.

A template is `Subject: …`, a blank line, then the body. Placeholders are
`{name}` with `name` in PLACEHOLDERS; `{{` and `}}` are literal braces. No
conditionals: `period_dash` / `period_for` are precomputed and empty when the
invoice has no period.
"""
import os
import re

import model
import store
from model import ValidationError

PLACEHOLDERS = ("invoice_number", "company_name", "customer_name", "period",
                "period_dash", "period_for", "total", "due_date", "invoice_date",
                "iban")

#: One token: an escaped brace, a `{name}` placeholder, or a stray brace.
_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
_SUBJECT = "Subject: "


def _split(text: str) -> tuple[str, str]:
    """(subject template, body template); ValidationError on a bad shape."""
    if not isinstance(text, str):
        raise ValidationError("email template: must be text")
    text = text.replace("\r\n", "\n")
    lines = text.split("\n", 2)
    if not lines[0].startswith(_SUBJECT):
        raise ValidationError("email template: the first line must start with 'Subject: '")
    if len(lines) < 2 or lines[1] != "":
        raise ValidationError("email template: the second line must be empty")
    return lines[0][len(_SUBJECT):], lines[2] if len(lines) > 2 else ""


def validate(text: str) -> None:
    """Refuse a bad shape, an unknown `{name}` or a stray single brace."""
    _split(text)
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok in ("{{", "}}"):
            continue
        name = m.group(1)
        if name is None:
            raise ValidationError(
                f"email template: stray {tok!r} (write {tok * 2!r} for a literal brace)")
        if name not in PLACEHOLDERS:
            raise ValidationError(
                f"email template: unknown placeholder {{{name}}} "
                f"(use {', '.join('{' + p + '}' for p in PLACEHOLDERS)})")


def load(customer: dict, plugin_root: str) -> str:
    """Customer `email_template` → data-dir templates/email.md → shipped
    templates/email.md. The chosen template is validated."""
    text = (customer or {}).get("email_template") or None
    if text is None:
        try:
            data_file = store.path("templates/email.md")
        except store.StoreError:
            data_file = None  # no CLAUDE_PLUGIN_DATA: skip this layer
        if data_file and os.path.isfile(data_file):
            with open(data_file, encoding="utf-8") as f:
                text = f.read()
    if text is None:
        with open(os.path.join(plugin_root, "templates", "email.md"), encoding="utf-8") as f:
            text = f.read()
    validate(text)
    return text


def render(text: str, values: dict) -> tuple[str, str]:
    """(subject, body). One pass: substituted values are never re-scanned."""
    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        if m.group(1) is None:
            raise ValidationError(f"email template: stray {tok!r}")
        try:
            return str(values[m.group(1)])
        except KeyError:
            raise ValidationError(
                f"email template: unknown placeholder {{{m.group(1)}}}") from None

    subject, body = _split(text)
    return _TOKEN.sub(sub, subject), _TOKEN.sub(sub, body)


def email_values(content: dict, invoice_number: str) -> dict:
    """Placeholder values from frozen invoice content, in the company locale."""
    company = content["company"]
    locale = company["locale"]
    label = model.period_label(content["period"]) if content.get("period") else ""
    return {
        "invoice_number": invoice_number,
        "company_name": company["name"],
        "customer_name": content["customer"]["name"],
        "period": label,
        "period_dash": " — " + label if label else "",
        "period_for": " for " + label if label else "",
        "total": model.format_money(content["totals"]["total"], company["currency"], locale),
        "due_date": model.format_date(content["due_date"], locale),
        "invoice_date": model.format_date(content["invoice_date"], locale),
        "iban": company["iban"],
    }
