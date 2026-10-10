---
id: opening-balances
name: Initial capital, assets, liabilities
category: transaction
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Opening balances (initial capital, assets, liabilities)

Posts the balances an entity starts with on its first day of accounting
(or on migration to this ledger): Dr every asset, Cr every liability, and
Cr the balancing amount to equity (share capital / retained earnings). The
entry MUST balance — assets == liabilities + equity.

## When to use

- "Set up opening balances", "migrate from the old system", "record the
  company formation"
- First day of accounting: bank balance, fixed assets, receivables,
  payables, loans, VAT liabilities, share capital
- Do NOT use for: ordinary day-to-day transactions, year-end closing
  entries (those net into retained earnings via the P&L).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| opening_date | date | yes | first day of the accounting period |
| assets[] | list | yes | each {code, name, balance_pence} — debit balances |
| liabilities[] | list | yes | each {code, name, balance_pence} — credit balances |
| equity_account_code | string | yes | `3000` Share Capital and/or `3020` Retained Earnings |
| equity_pence | integer | derived | Σ assets − Σ liabilities (must be ≥ 0 for a solvent start; a negative net worth goes to Dr Retained Earnings) |

## DR/CR construction

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (Bank) | balance | `1000` Bank Current Account, `1010` Bank Deposit Account |
| Dr | Asset (CurrentAsset) | balance | `1100` Trade Debtors, `1110` Prepayments, `1200` Stock |
| Dr | Asset (FixedAsset) | balance | `1300`–`1350` fixed assets AT COST (accumulated depreciation separately, below) |
| Cr | Asset (FixedAsset) | balance | `1360` Accumulated Depreciation — contra, credit balance |
| Cr | Liability (CurrentLiability) | balance | `2000` Trade Creditors, `2010` Accruals, `2210` Output VAT, `2220` Input VAT credit balance if any |
| Cr | Liability (LongTermLiability) | balance | `2100` Bank Loan |
| Cr | Equity (Equity) | balancing amount | `3000` Share Capital / `3020` Retained Earnings — Σ Dr − Σ Cr |

One row per account; the equity leg is the single balancing credit (or
debit for negative net worth).

## VAT treatment

- Opening VAT balances are NOT new VAT events: enter `2210` Output VAT
  (credit) and `2220` Input VAT (debit balance, contra-liability) at their
  brought-forward values. No VAT leg is calculated here.
- The engine's VAT periods pick up these balances from the accounts when
  the first return is computed.

## Formance interaction

[Dr 1000 150000, Dr 1300 50000, Dr 1100 25000, Cr 2000 30000, Cr 2210
5000, Cr 2100 40000, Cr 3000 150000] mirrors as six postings (greedy
pairing in posting order):

```json
[
  {"source": "2000", "destination": "1000", "amount": "300.00", "asset": "GBP/2"},
  {"source": "2210", "destination": "1000", "amount": "50.00", "asset": "GBP/2"},
  {"source": "2100", "destination": "1000", "amount": "400.00", "asset": "GBP/2"},
  {"source": "3000", "destination": "1000", "amount": "750.00", "asset": "GBP/2"},
  {"source": "3000", "destination": "1300", "amount": "500.00", "asset": "GBP/2"},
  {"source": "3000", "destination": "1100", "amount": "250.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

New limited company opening position (pence): bank £1,500.00, land &
buildings £500.00, trade debtors £250.00; trade creditors £300.00, output
VAT £50.00, bank loan £400.00; share capital balances the entry at
£1,500.00. In pence:

```dr-cr
Dr  1000 Bank Current Account  150000
Dr  1300 Land & Buildings       50000
Dr  1100 Trade Debtors          25000
Cr  2000 Trade Creditors        30000
Cr  2210 Output VAT              5000
Cr  2100 Bank Loan              40000
Cr  3000 Share Capital         150000
```

Debits 150000 + 50000 + 25000 = 225000; credits 30000 + 5000 + 40000 +
150000 = 225000. Balanced. Formance postings (see Formance interaction —
the six-posting list above matches this entry exactly).

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence — REQUIRED before the
  entry can post (422 otherwise)
- assets == liabilities + equity, i.e. the equity leg equals Σ Dr − Σ Cr
- every leg's code exists in the COA template; debit legs are Asset (or
  negative Equity) classes, credit legs are Liability, Equity, or
  contra-Asset (Accumulated Depreciation) classes
- exactly one equity balancing amount: a single credit leg (or a single
  debit leg for negative net worth); it is split across `3000` and `3020`
  only when the client provides both figures
- no Expense or Revenue leg in an opening-balances entry

## Edge cases

- Negative bank (overdraft) at start: credit the bank account instead of
  debiting it — an overdraft is a Liability in presentation
- Accumulated depreciation: Dr the asset at COST and Cr `1360` separately;
  never net them into one leg
- Retained losses: equity leg is a DEBIT to `3020` Retained Earnings when
  liabilities exceed assets
- Input VAT debit balance: `2220` Input VAT opens with a DEBIT balance
  (contra-liability) — that is correct, do not flip it to an asset
- Migration mid-year: opening balances plus a profit-and-loss b/f figure;
  equity = Σ assets − Σ liabilities, and the P&L b/f goes to `3020`

## Framework variants

- uk-gaap (FRS 102 §35): first-time adoption requires a statement of
  financial position at the date of transition — the opening entry must
  reconcile to the prior-year filed accounts.
- IFRS (IFRS 1): first-time adoption uses the same mechanics with an
  opening IFRS statement of financial position and reconciliation of
  equity; the journal shape is framework-neutral.
- us-gaap (ASC 250): opening entries must reconcile to the prior period's
  closing balance sheet — the DR/CR mechanics are framework-neutral. The
  US-specific rule is error correction: a material prior-period error is
  corrected by RESTATING the prior-period statements (retrospective
  restatement through opening retained earnings), while an immaterial
  error is corrected in the current period — a two-track rule FRS 102
  §10.21 approximates but applies with a different materiality practice.
  ASC 805 (business combinations — push-down or acquisition-date values)
  and ASC 830 (functional-currency translation) matter only as pointers
  for entities with those events. A US opening entry carries no VAT
  liability account — sales-tax payable replaces `2210` output VAT.
