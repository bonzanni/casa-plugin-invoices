# invoices

Invoicing for [Casa](https://github.com/bonzanni/ha-casa-app): your company and your
customers, invoice drafts reused from the previous invoice, numbered invoices with a
PDF rendered in-process, and a filled email ready to send. Everything is configured by
talking to the agent.

The plugin never sends email. On finalize it publishes the PDF to Casa's handoff folder
and returns the email (recipients, subject, body); the main assistant sends it with the
Gmail plugin, behind Gmail's own approval.

Successor to `lesina-invoice`, which was hard-wired to one sender and one customer and
used n8n for the PDF and the email.

## What it does

- One sender profile (your company) and any number of customers, each with billing
  details, VAT handling and the email recipients (to, cc, bcc) for their invoices.
- **"Invoice Acme like last time, no travel this month"**: a new draft copies the
  customer's last invoice, moves the period one month on, and you only say what
  differs. Drafts can also start from scratch or from any earlier invoice.
- Every draft is shown as a summary computed by the plugin; the agent never does the
  arithmetic. Amounts are kept in integer cents.
- A draft preview as a PDF marked DRAFT, sent in the chat.
- Finalize on your explicit approval of the exact summary you saw: the invoice gets
  the next number, its PDF is stored permanently and published to Casa's handoff
  folder, and the filled email comes back for sending.
- Python 3.11, standard library only: no third-party packages, nothing vendored, runs
  on `amd64` and `aarch64`.

Not included: sending email, payment tracking, reminders, credit notes, multiple
currencies, per-line VAT rates, more than one sender company.

## Requirements

- **Casa v0.376.1 or later**. The draft summary is a Markdown table that Casa shows
  on Telegram as a rich message: v0.376.0 added rich messages, and v0.376.1 keeps
  single line breaks, so the lines around the table and a line's detail under its
  description (a `<br>` in the cell) display as intended. Older releases show the
  `<br>` literally. The plugin file handoff (`/data/handoff`, where finalized PDFs are
  published) and plugin media (`/data/plugin-outbox`, used for draft previews) came
  earlier, in v0.326.0. Casa runs the server with its own `python3` (3.11).
- **The Gmail plugin (`casa-plugin-gmail`) 0.14.0 or later** for the send step:
  `send_email` with `cc` and `bcc`. The invoice email's `from_address` works when it is
  a send-as alias of the Gmail account; otherwise the email is sent from the account's
  own address.

## Installing

The plugin is meant for the finance specialist (the agent the main assistant delegates
money matters to). Ask Casa's assistant in chat, for example: *"Install the invoices
plugin from bonzanni/casa-plugin-invoices for the finance specialist."* Casa's
configurator installs the latest release and asks for your consent. No environment
variables or secrets are needed.

## First-time setup

Ask the finance specialist to set up invoicing. It asks, in one go, only for what is
missing: company name and address, VAT and registration ids (with the labels to print,
e.g. "BTW-id" and "KvK"), email, IBAN and bank, payment terms, default VAT rate, and
**the last invoice number you already used** in the series, so numbering continues
after it. Then tell it about your customers: name, address, ids, the email recipients,
and any VAT override (a customer at 0% gets a note such as "VAT reverse-charged").

Change anything later the same way ("Acme's invoices also go to their accountant in
cc"). Number formatting follows the company's locale (`nl-NL`, `de-DE`, `en-GB`,
`en-US`).

The email template is plain text with fixed placeholders (`{invoice_number}`,
`{company_name}`, `{customer_name}`, `{period}`, `{total}`, `{due_date}`,
`{invoice_date}`, `{iban}`, `{period_dash}`, `{period_for}`). The shipped default is
[`templates/email.md`](templates/email.md); a customer can carry its own.

## The flow

```
you ──"invoice Acme for November, like last time, no travel"──▶ main assistant
main assistant ──delegates──▶ finance specialist (has this plugin)
finance specialist: invoice_new → invoice_edit → summary with Draft and Ref
you ──"approve"──▶ invoice_finalize                         [Casa approval tap]
finance specialist ──returns the package──▶ main assistant
main assistant: shows recipients, subject, attachment;
                Gmail send_email(to, cc, bcc, subject, body, from_address,
                attachment_paths=[pdf_path], request_id)    [Gmail approval tap]
```

The package is `{invoice_number, pdf_path, email {to, cc, bcc, from_address, subject,
body}, request_id, total}`. `request_id` lets Gmail refuse a duplicate send of the
same invoice. The handoff copy of the PDF expires after 7 days; asking for the invoice
again (`invoice_package`) republishes the stored PDF and returns the same package.

Finalize, discarding a draft and removing a customer are protected: Casa shows you
an approval button before they run.

## Numbering

- Pattern with `{YYYY}` and `{seq:N}` tokens, default `INV-{YYYY}-{seq:4}`. One series
  per year: the sequence restarts at 1 in a new year.
- The number is assigned **at finalize**, never to a draft, so a discarded draft never
  leaves a gap.
- The files are the truth: the next number is one past the highest issued number of
  the year, and never below the floor set at setup (the last number you already used).
- Finalize runs under a lock shared by every session and writes a recoverable claim
  first; if it is interrupted, the next finalize completes that invoice before
  assigning any new number. Finalizing the same draft twice returns the same invoice.

## Data

Kept under the plugin's data directory (`CLAUDE_PLUGIN_DATA`), plain JSON, written
atomically and durably:

| Path | Content |
|---|---|
| `company.json` | Sender profile and numbering |
| `customers/<id>.json` | One file per customer |
| `drafts/<draft_id>.json` | Open drafts |
| `invoices/<number>.json` | Issued invoices, immutable, with a snapshot of the company and customer details as issued |
| `invoices/<number>.pdf` | The issued PDF, kept permanently |
| `finalizing/<number>.json` | Present only while a finalize is in flight |
| `templates/email.md` | Optional override of the shipped email template |

## Development

Python 3.11, standard library only.

```bash
python3.11 -m unittest discover -s tests   # unit tests
python3.11 tests/e2e_smoke.py              # real server over stdio: setup, draft, preview, finalize
```

`server/money.py` and `server/casa_handoff.py` are verbatim copies from Casa's
bank-feed plugin; see [`server/VENDORED.md`](server/VENDORED.md) before touching them.

## License

MIT, see [LICENSE](LICENSE).
