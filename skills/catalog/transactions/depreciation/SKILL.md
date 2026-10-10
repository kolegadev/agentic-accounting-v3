---
id: depreciation
name: Monthly straight-line depreciation
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Monthly straight-line depreciation

Charges a fixed asset's cost to the P&L over its useful life:
Dr Depreciation (expense) / Cr Accumulated Depreciation (contra-asset).
NEVER credit the asset account itself — the asset stays at cost and the
contra account accumulates the write-down.

## When to use

- "Run monthly depreciation", "depreciate the equipment", "post
  depreciation for April"
- Month-end charge for fixed assets: `1320` Computer Equipment, `1310`
  Office Equipment, `1330` Motor Vehicles, `1340` Plant & Machinery
- Do NOT use for: asset purchase (supplier-invoice with the Asset class),
  asset disposal (see Edge cases), revaluation (out of scope v0),
  amortisation of intangibles (out of scope v0).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| asset_account_code | string | yes | the FixedAsset account holding cost, e.g. `1320` Computer Equipment |
| cost_pence | integer | yes | gross cost of the asset, integer pence |
| residual_pence | integer | no | expected scrap value (default 0) |
| useful_life_months | integer | yes | depreciable life, e.g. 60 for a 5-year computer |
| period_end_date | date | yes | month the charge belongs to |
| method | enum | yes | `straight_line` (only method in v0) |

## DR/CR construction

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | monthly_charge | `6500` Depreciation — P&L charge |
| Cr | Asset (FixedAsset) | monthly_charge | `1360` Accumulated Depreciation — contra-asset, credit balance |

monthly_charge = round((cost − residual) ÷ useful_life_months) in pence.
The asset account (`1320` etc.) is NEVER credited — its balance stays at
cost; the contra account `1360` accumulates the credit side.

## VAT treatment

None. Depreciation is a non-VAT journal — VAT was accounted for when the
asset was purchased (and the asset's carrying amount excludes recoverable
input VAT). No input or output VAT leg ever appears here.

## Formance interaction

Balanced 2-leg entry [Dr 6500 5000, Cr 1360 5000] produces exactly one
posting — source is the credit (accumulated depreciation) account,
destination the debit (depreciation expense):

```json
[
  {"source": "1360", "destination": "6500", "amount": "50.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Computer equipment cost £3,000.00 (`1320`), 5-year life, no residual,
straight-line. Monthly charge = 300000 ÷ 60 = 5000 pence = £50.00:

```dr-cr
Dr  6500 Depreciation                5000
Cr  1360 Accumulated Depreciation     5000
```

Debits 5000 == credits 5000. One Formance posting:
`{source: 1360 → destination: 6500, "50.00", asset: GBP/2}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence
- exactly two legs: Dr `6500` Depreciation (Expense class), Cr `1360`
  Accumulated Depreciation (Asset/FixedAsset class) — the Cr leg must NEVER
  be the asset account itself (`1300`–`1350`)
- monthly_charge == round((cost − residual) ÷ life_months)
- accumulated depreciation never exceeds cost − residual (asset not
  depreciated below its residual value)

## Edge cases

- Residual value: depreciate only (cost − residual); stop when the net book
  value reaches residual
- Part month: prorate by days for mid-month acquisitions (first/last month
  partial charges)
- Rounding: keep pence-precision; the final month absorbs rounding so total
  charges equal exactly cost − residual
- Disposal: Dr Bank (proceeds) + Dr `1360` (clear accumulated) / Cr the
  asset account at cost; the difference goes to a gain/loss on disposal
  expense account
- Fully depreciated asset: stop posting — a further charge would push
  accumulated depreciation above cost
- Impairment: a separate Dr impairment-expense / Cr asset (or contra)
  entry — out of scope v0

## Framework variants

- uk-gaap (FRS 102 §17 / FRS 105): straight-line over useful life is the
  standard method; FRS 105 micro-entities hold assets at cost with no
  revaluation and no component split.
- IFRS (IAS 16): component depreciation (significant parts depreciated
  separately), annual review of useful life and residual, and a revaluation
  model option — the monthly charge shape is the same, but the input
  (component lives) differs for larger assets.
- us-gaap: TODO — ASC 360-10 uses similar straight-line mechanics but
  different component conventions and no annual useful-life review
  requirement mirroring IAS 16; book depreciation also diverges from
  MACRS tax depreciation, which is never booked to the ledger.
