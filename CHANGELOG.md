# Changelog

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
