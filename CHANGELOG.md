# Changelog

## 0.2.1 — 2026-11-16

Casa floor: v0.326.0 (unchanged).

- PDF: the QTY, UNIT PRICE and AMOUNT columns are sized from the invoice's own
  numbers. Large amounts (ten digits and more) no longer overprint the unit price,
  and long fractional quantities no longer run into the description. Invoices with
  ordinary numbers keep the same layout.
- PDF: a multi-page invoice carries "Invoice <number> · Page i of n" (drafts:
  "Draft invoice · Page i of n") under the footer of every page.
- PDF: more room between the VAT row and the rule under it. Totals labels wrap
  beside their amounts, which stay full size unless a sum is extraordinarily long.
- Company profile: `tax_name` is at most 30 characters (VAT, BTW, MwSt, ...), so the
  totals block always fits on a page.

## 0.2.0 — 2026-11-16

Casa floor: v0.326.0 (unchanged).

- The one-time import from `lesina-invoice` is removed from the plugin: the
  `invoice_import_legacy` tool, its approval entry and its README section are gone.
  The plugin now has 14 tools.

## 0.1.0 — 2026-11-16

Casa floor: v0.326.0 (plugin file handoff). The send step needs the Gmail plugin
0.14.0 or later (cc/bcc).

First release. Successor to `lesina-invoice`, for any Casa owner's company and
customers.

- Company profile and customers (billing details, VAT rate or note, email to/cc/bcc,
  per-customer email template), set up and changed by conversation.
- Drafts from the customer's last invoice (period moved one month on), from any earlier
  invoice, or from scratch; all-or-none edits; server-rendered summaries with a `Ref`
  guard so only the approved content is issued.
- Numbering assigned at finalize under a cross-process lock, with a recoverable claim
  and resume; one series per year; idempotent per draft.
- In-process PDF (standard library only, A4, Helvetica), draft previews marked DRAFT
  via Casa's media outbox.
- Finalized PDFs stored permanently and published to Casa's handoff folder; the package
  carries a filled plain-text email for the Gmail plugin's `send_email` (cc/bcc need
  Gmail 0.14.0 or later). `invoice_package` republishes after the 7-day handoff expiry.
- One-time import from `lesina-invoice`. Cutover: re-save one default in the old
  plugin, resolve any pending submission, disable the n8n invoice workflows, install
  and import, then uninstall `lesina-invoice` (README, "Moving from lesina-invoice").
