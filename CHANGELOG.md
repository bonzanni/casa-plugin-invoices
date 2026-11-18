# Changelog

## 0.3.0 — 2026-11-18

Casa floor: v0.376.1 (was v0.326.0). The summary is a table that Casa shows on
Telegram as a rich message (v0.376.0), and it needs v0.376.1's line breaks for the
lines around the table and for a line's detail, which older releases show as a
literal `<br>`. Not backward compatible with older Casa.

- The draft summary shows its lines as a table (Item, Qty, Unit price, Amount, the
  amounts right-aligned) with Subtotal, VAT and a bold Total as its last rows, instead
  of a fixed-width block in a code box. Descriptions are shown in full (they were cut
  at 22 characters) and a line's detail sits under its description in the same cell.
- Markdown characters in customer names, descriptions, details and notes are escaped,
  so a `|` or `*` in a description shows as written and cannot break the table.
- The skill tells the agent to show invoice, draft and customer lists, and a fetched
  invoice's lines, as a narrow table with named columns.
- The Ref is unchanged: it still covers the invoice content, not the summary text, so
  approvals given on 0.2.x summaries match the same drafts.

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
- Numbering: a pattern holds at most 20 characters besides `{YYYY}` and `{seq:N}`,
  so invoice numbers stay readable in the header and page labels.

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
