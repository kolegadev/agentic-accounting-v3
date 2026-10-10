---
id: payroll
name: Net pay + PAYE/NIC liabilities
category: transaction
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Payroll (gross pay, net pay, PAYE and NIC)

Records a payroll run: Dr Salaries for the GROSS pay, Cr Bank for the NET
pay, and Cr the withholding liabilities (PAYE income tax and employee NIC)
that the employer pays to HMRC later. Employer's NIC is a separate
expense: Dr Employer's NI / Cr the same PAYE/NIC liability account.

## When to use

- "Run payroll", "post the monthly salary", "record wages for June"
- Monthly/weekly payroll run with gross, PAYE, employee NIC, employer NIC
- Do NOT use for: paying HMRC the collected PAYE/NIC (that is a simple
  Dr `2030` / Cr Bank), pension scheme settlements (Dr the pension-payable
  liability / Cr Bank), directors' dividends.

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| period_end_date | date | yes | payroll period end |
| pay_date | date | yes | date net pay leaves the bank |
| gross_pence | integer | yes | gross salary/wages, integer pence |
| paye_pence | integer | yes | income tax withheld under PAYE |
| employee_nic_pence | integer | yes | employee's National Insurance withheld |
| employer_nic_pence | integer | yes | employer's NI — extra cost on top of gross |
| pension_employee_pence | integer | no | employee pension deduction (via net pay) |
| pension_employer_pence | integer | no | employer pension contribution (extra expense) |
| pension_payable_account_code | string | no | client-created pension-payable liability, e.g. `2041` Pension Payable (category Liability / type CurrentLiability) — MUST exist in the client's COA before a payroll-with-pension run; required whenever either pension field is set |
| bank_account_code | string | yes | `1000` Bank Current Account |

Derived: net_pence = gross − paye − employee_nic − pension_employee.
Total bank outflow = net pay only. Liabilities (PAYE + employee NIC +
pension deductions + employer NIC) are settled to HMRC/pension provider in
separate later transactions.

## DR/CR construction

Salary journal (gross to net):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | gross_pence | `6600` Staff Wages — the full cost of employing |
| Cr | Asset (Bank) | net_pence | `1000` Bank Current Account — what the employee receives |
| Cr | Liability (CurrentLiability) | paye_pence | `2030` PAYE / NI Payable — PAYE withheld for HMRC |
| Cr | Liability (CurrentLiability) | employee_nic_pence | `2030` PAYE / NI Payable — employee NIC withheld for HMRC |

Employer's NIC journal:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | employer_nic_pence | `6610` Employer's NI — a cost in addition to gross |
| Cr | Liability (CurrentLiability) | employer_nic_pence | `2030` PAYE / NI Payable — due to HMRC |

Pension (when present): Dr `6630` Pension Contributions for the employer
share / Cr the pension-payable liability; the employee share reduces net
pay and is a further Cr to the same pension-payable account. NO
pension-payable account exists in any COA template — the client MUST
create one in their COA before any payroll-with-pension run: name it
`2041` Pension Payable, category Liability / type CurrentLiability
(`2040` is already Corporation Tax Payable in the default template).
Pension NEVER routes through `2030`, which stays exactly equal to the
HMRC PAYE/NIC debt.

## VAT treatment

None. Wages and salaries are outside the scope of VAT — no input or output
VAT leg ever appears in a payroll entry.

## Formance interaction

Salary journal [Dr 6600 250000, Cr 1000 215000, Cr 2030 22000, Cr 2030
13000] mirrors as three postings (greedy pairing: one debit leg against
three credit legs in order):

```json
[
  {"source": "1000", "destination": "6600", "amount": "2150.00", "asset": "GBP/2"},
  {"source": "2030", "destination": "6600", "amount": "220.00", "asset": "GBP/2"},
  {"source": "2030", "destination": "6600", "amount": "130.00", "asset": "GBP/2"}
]
```

Employer's NIC [Dr 6610 15000, Cr 2030 15000]:

```json
[
  {"source": "2030", "destination": "6610", "amount": "150.00", "asset": "GBP/2"}
]
```

Pension variant salary journal [Dr 6600 250000, Cr 1000 210000, Cr 2030
22000, Cr 2030 13000, Cr 2041 5000] mirrors as four postings (greedy
pairing: one debit leg against four credit legs in order):

```json
[
  {"source": "1000", "destination": "6600", "amount": "2100.00", "asset": "GBP/2"},
  {"source": "2030", "destination": "6600", "amount": "220.00", "asset": "GBP/2"},
  {"source": "2030", "destination": "6600", "amount": "130.00", "asset": "GBP/2"},
  {"source": "2041", "destination": "6600", "amount": "50.00", "asset": "GBP/2"}
]
```

Employer pension [Dr 6630 7500, Cr 2041 7500]:

```json
[
  {"source": "2041", "destination": "6630", "amount": "75.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

June payroll: gross £2,500.00; PAYE £220.00; employee NIC £130.00; net pay
£2,150.00; employer's NIC £150.00. In pence:

```dr-cr
Dr  6600 Staff Wages      250000
Cr  1000 Bank Current Account  215000
Cr  2030 PAYE Payable      22000
Cr  2030 NIC Payable       13000
```

Debits 250000 == credits 215000 + 22000 + 13000 = 250000. Formance
postings: `{1000 → 6600, "2150.00"}`, `{2030 → 6600, "220.00"}`,
`{2030 → 6600, "130.00"}`.

Employer's NIC:

```dr-cr
Dr  6610 Employer's NI  15000
Cr  2030 NIC Payable     15000
```

Debits 15000 == credits 15000. One Formance posting:
`{2030 → 6610, "150.00"}`.

Pension variant (auto-enrolment): employee pension £50.00 (deducted from
net pay) and employer pension £75.00. Net pay = 250000 − 22000 − 13000 −
5000 = 210000 pence. Salary journal:

```dr-cr
Dr  6600 Staff Wages          250000
Cr  1000 Bank Current Account  210000
Cr  2030 PAYE Payable          22000
Cr  2030 NIC Payable           13000
Cr  2041 Pension Payable        5000
```

Debits 250000 == credits 210000 + 22000 + 13000 + 5000 = 250000. Four
Formance postings: `{1000 → 6600, "2100.00"}`, `{2030 → 6600, "220.00"}`,
`{2030 → 6600, "130.00"}`, `{2041 → 6600, "50.00"}`.

Employer pension journal:

```dr-cr
Dr  6630 Pension Contributions  7500
Cr  2041 Pension Payable         7500
```

Debits 7500 == credits 7500. One Formance posting:
`{2041 → 6630, "75.00"}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence, in every journal
- gross == net + PAYE + employee NIC (+ employee pension) — exact pence
- the only Expense legs are `6600` Staff Wages (gross), `6610`
  Employer's NI, and `6630` Pension Contributions (employer share only);
  the bank leg credits exactly net pay, never gross
- every withholding leg credits a Liability (CurrentLiability) class;
  pension legs credit the client-created pension-payable liability (e.g.
  `2041` Pension Payable), never `2030`
- the `2030` balance after the run equals the amount owed to HMRC
  (pension never routes through `2030`)

## Edge cases

- Net pay ≤ 0 (attachment of earnings, deductions exceed pay): bank leg is
  zero — post Dr Wages / Cr the liability accounts only
- Pension salary sacrifice: gross is REDUCED by the sacrificed amount
  before PAYE/NIC — feed the reduced gross into this schema
- Leavers (P45) and mid-period joins: prorate gross and recompute PAYE/NIC
  per HMRC tables; the DR/CR shape is unchanged
- Year-end accrual: wages earned but unpaid at period end — Dr `6600` /
  Cr `2010` Accruals (see journal-accrual skill)
- Directors' dividends: NOT payroll — Dr `3030` Dividends Paid / Cr Bank

## Framework variants

- uk-gaap (FRS 102 §28): short-term employee benefits are expensed when
  the employee renders service; the payroll posting is identical.
- IFRS (IAS 19): short-term benefits are recognised as the service is
  rendered, with an accrual for unpaid wages and holiday pay at period end
  — IAS 19's accrued-holiday requirement is explicit, FRS 102 §28 achieves
  the same via the accrual skill; the payroll posting itself is identical.
- us-gaap (ASC 710): gross pay is expensed as the employee renders
  service — the posting shape is identical, but every tax differs from
  PAYE/NIC. FICA: Social Security 6.2% + Medicare 1.45%, each charged to
  BOTH employee (withheld from gross) and employer (employer-expense),
  plus a 0.9% employee-only Medicare surtax above the high-earner
  threshold. FUTA (federal unemployment) is employer-only — 6.0% on the
  first $7,000 of wages, 0.6% effective with the standard state credit;
  SUTA (state unemployment) is employer-only at state-set rates. Federal
  and state income tax are withheld from gross. All withheld amounts are
  liabilities remitted on Form 941 (quarterly) and Form 940 (annual);
  401(k) deferrals replace auto-enrolment pensions — the `2030` PAYE/NIC
  account is a UK construct with no US equivalent.
