# Passdown — Bank Charges feature (+ FX fix)

> Handoff for a fresh chat. Everything needed to implement the bank-charge feature
> is here, including the **empirically verified** Payment Entry recipe. Read the
> "VERIFIED RECIPE" and "IMPLEMENTATION PLAN" sections first.

## Goal (organizer feedback)
The AI should look up a **bank charge rate**, compute the fee, and write it into the
Payment Entry as a **separate line** — so reconciliation is "not just forex". The fee
and the forex gain/loss must be booked as **distinct** GL lines.

## Decisions already confirmed with the user
1. **Rate source:** a **new tab in the existing Google Sheet** (same spreadsheet as the
   bank statement, `config.BANK_STATEMENT_SHEET_URL`). Suggested tab name `BankCharges`.
   The user will create this tab. Schema (their final version):

   | Currency | Percentage | FlatRate(MYR) | Note |
   |----------|-----------|---------------|------|
   | USD | 0.05 | 25 | SWIFT + intermediary |
   | SGD | 0 | 10 | Regional TT |
   | EUR | 0.05 | 25 | SWIFT + intermediary |
   | TWD | 0 | 15 | Regional TT |
   | * | 0.05 | 20 | Default SWIFT |

   - Lookup key = **payment currency** (the invoice's foreign currency). `*` = fallback.
   - **`Percentage` is in percent, and `0.05` means `0.05%`** (NOT 5%). Confirmed by user.
   - **Fee formula:** `charge_myr = gross_myr * (Percentage/100) + FlatRate`
   - `Note` column is ignored by the parser.
   - Read it the same way the bank statement is read: `bank_statement_parser.fetch_sheet(sheet_url, "BankCharges")`
     uses gviz CSV (`/gviz/tq?tqx=out:csv&sheet=<tab>`). A small dedicated parser is cleaner
     than reusing the bank-statement column detector (different columns).

2. **Write into ERPNext accounting?** YES — the user wants it booked (they pointed to a
   working example `ACC-PAY-2026-00971` that has a negative FX deduction). We verified a
   recipe that books all four lines correctly (below).

## Grounding data (already queried from the live ERPNext — no need to re-query)
Company: `Penang Components Sdn Bhd` (abbr PCSB), company currency **MYR**.
- Bank Charges account: **`5221 - Bank Charges - PCSB`** (Expense)
- Exchange Gain/Loss account: `5219 - Exchange Gain/Loss - PCSB`
- Cost Center: `Main - PCSB`
- Bank GL account: `1210 - Maybank - PCSB` (MYR)
- **There are NO `Bank Account` doctype records** — the current `create_payment_entry`
  bank lookup (`erp.list("Bank Account", ...)`) returns empty and `paid_to` ends up "".
  Use the **Bank-type GL account** directly (`1210 - Maybank - PCSB`) instead. Worth fixing.
- **There are NO `Currency Exchange` records** — this is why ERPNext mishandles FX on
  cross-currency payments unless we feed `source_exchange_rate` ourselves (see findings).
- Sample foreign invoices used for testing:
  - `ACC-SINV-2026-00731` — Micron US, **USD 203.52**, conversion_rate **4.0**, book = MYR 814.08, debit_to `1312 - Debtor-USD - PCSB`
  - `ACC-SINV-2026-00730` — Nvidia Singapore, SGD 424, rate 3.0, debit_to `1311 - Debtor-SGD`
  - `ACC-SINV-2026-00733` — Micron US, USD 1123.6, rate 4.0

## What's wrong today (root cause)
The existing `create_payment_entry` (tools.py:871) sets `source_exchange_rate` =
`get_rate(payment_date)` (the **payment-day** rate). On this instance that causes:
- The receivable (Debtor) is cleared at the **payment rate**, not the invoice book rate
  → **over-cleared**.
- **No exchange gain/loss is booked at all.** (Verified: a baseline PE with payment-rate
  source produced GL `Dr Maybank 834.43 / Cr Debtor-USD 834.43` and nothing else — the
  20.35 FX gain vanished.) So the current "forex loss" feature isn't actually posting FX.

## EXPERIMENTS RUN (all on the live ERPNext, all cleaned up / cancelled)
Reference invoice: USD 203.52 @ book rate 4.0 (book = MYR 814.08). Assume payment-day
rate 4.10 → gross = 834.43; USD bank charge = 834.43*0.0005 + 25 = **25.42**; net received
= 834.43 − 25.42 = **809.01**; true FX gain = gross − book = 834.43 − 814.08 = **20.35**.

| Variant | source_rate | received | deductions | Result |
|---|---|---|---|---|
| Baseline (current code) | 4.10 (pay) | 834.43 gross | none | WRONG: Cr Debtor 834.43, no FX |
| Gross + charge | 4.10 | 834.43 | [charge 25.42] | WRONG: Debtor over-credited 859.85, no FX |
| Net + charge | 4.10 | 809.01 | [charge 25.42] | WRONG: Debtor 859.85, FX **loss** 25.42 (wrong sign) |
| Net + charge, book rate | 4.0 (book) | 809.01 | [charge 25.42] | WRONG: Debtor 839.50 (over by charge), FX 5.07 |
| **TX_FUDGE (WINNER)** | **(book−charge)/foreign = 3.8751** | **809.01 net** | **[charge +25.42]** | ✅ CORRECT (see below) |
| Sign test: charge **−25.42** | 3.8751 | 809.01 | [charge −25.42] | ❌ ERPNext rejects: "Difference Amount must be zero" |

### Why TX_FUDGE works (the key insight)
ERPNext's Receive formula is effectively:
- `Debtor cleared (Cr) = base_paid_amount + sum(non_FX deductions)`
- `auto FX deduction = base_paid_amount − base_received_amount` (auto-added, correct sign)

So if we set `base_paid = book − charge`, the charge "adds back" and Debtor clears at
exactly `book`. And the charge **cancels out** of the FX calc:
`auto_FX = (book − charge) − (gross − charge) = book − gross = −20.35` (gain). Generalizes
to any amount.

### Verified-correct GL (TX_FUDGE, submitted then cancelled)
```
Dr  1210 Maybank                809.01   (net cash actually received)
Dr  5221 Bank Charges            25.42   (the fee — POSITIVE deduction)
Cr  1312 Debtor-USD             814.08   (788.66 + 25.42 = book; cleared correctly)
Cr  5219 Exchange Gain/Loss      20.35   (forex gain; ERPNext auto-added, correct sign)
```
Balances: Dr 834.43 = Cr 834.43. Matches the user's mental model (book 800, recv-equiv
810, charge −20, forex +10).

### Sign convention (user asked about this)
In the `deductions` child table: **positive amount = expense → Dr; negative amount =
gain → Cr.**
- **Bank charge is an EXPENSE → enter POSITIVE** (`+25.42`). Negative fails validation.
- Forex GAIN is entered NEGATIVE (that's the `−0.31` the user saw in `ACC-PAY-2026-00971`);
  forex LOSS would be positive. Bank charge and forex are opposite signs — that's correct.

## VERIFIED RECIPE (pseudocode for create_payment_entry, Receive + foreign currency)
```
foreign      = invoice.outstanding_amount            # e.g. 203.52 (USD)
inv_rate     = invoice.conversion_rate               # e.g. 4.0  (book rate)
book_myr     = foreign * inv_rate                     # 814.08
pay_rate     = get_rate(inv_currency, "MYR", pay_date) # 4.10 (forex API, as today)
gross_myr    = round(foreign * pay_rate, 2)           # 834.43
pct, flat    = get_bank_charge_rate(inv_currency)     # from BankCharges sheet -> (0.05, 25)
charge       = round(gross_myr * pct/100 + flat, 2)   # 25.42
net_received = round(gross_myr - charge, 2)           # 809.01
base_paid    = round(book_myr - charge, 2)            # 788.66
source_rate  = round(base_paid / foreign, 6)          # 3.875098  <-- the "fudge"

pe.source_exchange_rate = source_rate
pe.received_amount      = net_received
pe.paid_to              = "1210 - Maybank - PCSB"     # bank GL acct (Bank Account doctype empty!)
pe.deductions = [{ account: "5221 - Bank Charges - PCSB",
                   cost_center: "Main - PCSB",
                   amount: charge }]                  # POSITIVE
# ERPNext auto-adds the 5219 Exchange Gain/Loss deduction with correct sign.
```
Notes / open questions to decide while implementing:
- **MYR (same-currency) invoices:** no FX. Domestic — probably no SWIFT charge. Keep the
  existing simple path (rate 1.0). Decide whether any flat charge applies (TWD/SGD are
  "Regional TT" flat fees but those are still foreign currencies, so they hit the FX path).
- **Pay (supplier) direction:** the recipe above is for Receive. Mirror for Pay
  (payment_type "Pay") — signs/accounts differ; TEST it the same way before trusting.
- Decide whether `charge` percentage applies to `gross_myr` (current assumption) or the
  net/received. We used gross.

## IMPLEMENTATION PLAN
1. **`bank_statement_parser.py`** (or a new small helper): function to fetch + parse the
   `BankCharges` tab → dict `{currency: {"percent": float, "flat": float}}` with a `*` default.
2. **`tools.py`**:
   - New tool `get_bank_charge_rate` (schema + dispatcher branch) → returns
     `{currency, percent, flat, charge}` for a given currency (+ optional amount).
   - Rewrite `create_payment_entry` (tools.py:871) to use the VERIFIED RECIPE: compute
     charge, set `source_exchange_rate`/`received_amount` per recipe, add the bank-charge
     deduction, and switch `paid_to` to the Bank-type GL account (`1210 - Maybank`) since
     the Bank Account doctype is empty.
   - This also FIXES the existing FX bug (clears Debtor at book + books FX correctly).
3. **UI**: in the three-way card / reconciliation report (app.py — `render_three_way_card`,
   and the recon-report builder `tools.py:1025+` / app.py:889 "Losses (FX + bank charges)"),
   split **Bank charge** and **Forex gain/loss** into two distinct figures (currently lumped).
4. **`domains.py`** prompt (the AR/accounting domain, ~line 100-168): tell the agent to call
   `get_bank_charge_rate` during reconciliation and to report bank charge and forex
   separately. Add `get_bank_charge_rate` to the domain's tool list.
5. **`get_payment_forex_loss`** (tools.py:952) already reads the 5219 deduction by
   `is_exchange_gain_loss` — should now return real numbers once FX is booked. Verify.
6. **Test** end-to-end with `ACC-SINV-2026-00731` via the "create draft → check GL →
   cancel" loop (user authorized this; they'll help delete cancelled PEs).

## Cleanup owed
Leftover **cancelled (docstatus=2)** test Payment Entries that can't be hard-deleted via API
(LinkExistsError — they have GL links). Ask the user to delete them in the ERPNext desk, or
leave them (they're inert):
`ACC-PAY-2026-01002, 01003, 01004, 01005, 01006, 01007` (all Micron US, docstatus=2).
Plus `ACC-PAY-2026-01008` from the final implementation test (also Micron US, docstatus=2).
Invoice `ACC-SINV-2026-00731` was restored to Overdue / outstanding 203.52 after each test.

## STATUS: IMPLEMENTED (2026-06-03) — final approach, TX_FUDGE SCRAPPED
The whole TX_FUDGE premise was WRONG: ERPNext on this instance DOES book realized FX correctly
on its own (via a linked auto Journal Entry against the Payment Entry — we'd missed it because we
only queried GL by the PE's own `voucher_no`). The fudge "fixed" a non-problem and caused real
bugs (a 6.55 `unallocated_amount` phantom + a fake exchange rate leaking into the report).

The shipped solution: a single **Payment Entry** that books the bank charge AND the forex as
distinct lines in its **"Deductions or Loss"** table (NO separate Journal Entry — the user
specifically wanted it on the loss side, like the ERPNext desk UI). Discovered from a desk
screenshot.
- `source_exchange_rate` = the REAL payment-day rate from `forex.get_rate()` (the API). NOTE:
  an earlier belief that "real rate + charge-on-loss-side CONFLICT" was WRONG — they coexist
  fine. With the real rate, base_paid < book, so ERPNext re-sizes the FX deduction row and tops
  up the remainder (book − base_paid) with its own linked FX Journal Entry. The charge still
  lives in the PE deductions; no separate charge JE.
- `received_amount` = the actual NET MYR received (from the bank statement / proof).
- `deductions` (3 rows; ERPNext adjusts the FX row to balance):
  ① `Exchange Gain/Loss` = gap = book − net, `is_exchange_gain_loss=1` (ERPNext re-sizes this to
     base_paid − net and books the rest as the linked FX JV);
  ② `Bank Charges` = the SWIFT/TT fee (from the BankCharge sheet) — its own GL line;
  ③ `Exchange Gain/Loss` = −fee, UNflagged counter that cancels ② in the over-allocation calc.
  Net realized forex = (PE exchange-account deductions) + (linked FX JV) = gap − fee.

Why the counter row: a plain bank-charge deduction (non-FX) inflates ERPNext's
`unallocated_amount` and over-clears the invoice; the −fee counter (also non-FX) cancels it,
while the FX row is FX-flagged and thus excluded from that calc.

Files: `config.py` (+`EXCHANGE_GL_ACCOUNT`); `bank_statement_parser.fetch_bank_charges()` reads the
**`BankCharge`** tab (singular!) → `{currency:{percent,flat}}` with `*` default; `tools.py`
(`get_bank_charge_rate` tool + `lookup_bank_charge()` helper; `create_payment_entry` = 3-row recipe,
Receive/Sales-Invoice only — supplier Pay returns "not implemented"; `get_payment_forex_loss` and
`generate_reconciliation_report` compute realized forex = PE exchange-account deductions + the
linked FX JV, and the bank charge by account); `app.py` (recon PDF + batch PDF show Bank charge +
Forex as two rows); `domains.py` (`get_bank_charge_rate` in tool list + prompt reports the two
separately, EXACTLY as returned — no recompute/discrepancy). Domestic MYR keeps the simple PE path.

Live-verified (real rate 3.9525, net received 778.50): `source_exchange_rate` = 3.9525; deductions
[Exchange 25.91(fx), Bank Charges 25.39, Exchange −25.39]; PE GL `Dr Maybank 778.50 + Bank Charges
25.39 + Exchange 25.91 / Cr Debtor 804.41 + Exchange 25.39`, linked FX JV `Dr Exchange 9.67` →
Debtor clears 814.08 book; net forex 10.19 loss; invoice → Paid, `unallocated_amount` = 0.

NOT done (out of scope): supplier **Pay** direction; the read-only `suggested_payment_entry` preview
inside `three_way_reconcile` (actual posting goes through `create_payment_entry`, which is correct).

## How to verify ERPNext quickly (snippet)
```python
import httpx, config, json
h={'Authorization': f'token {config.ERPNEXT_API_KEY}:{config.ERPNEXT_SECRET}'}
base=config.ERPNEXT_URL.rstrip('/')
# e.g. read a PE: httpx.get(base+'/api/resource/Payment Entry/<name>', headers=h)
# create: POST {'data': pe} to /api/resource/Payment Entry
# submit: PUT {'data':{'docstatus':1}} to /api/resource/Payment Entry/<name>
# GL:     GET /api/resource/GL Entry?filters=[["voucher_no","=","<name>"]]&fields=[...]
# cancel: POST /api/method/frappe.client.cancel {'doctype':'Payment Entry','name':'<name>'}
```
