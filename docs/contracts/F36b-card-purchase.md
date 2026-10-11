# CONTRACT F36b — Card Purchase Skill (cash-basis purchase paid immediately)

## Goal

John's live test surfaced a catalog gap the mentor itself flagged: "I
don't have a skill for a purchase paid immediately from the bank (card
payment) — my purchase skills record invoices on credit
(supplier-invoice) and payments against an existing supplier balance
(supplier-payment)."  Add a `card-purchase` skill: a purchase (expense OR
asset) paid immediately by card/debit from a bank account, on the
cash/debit basis — no creditors involved, VAT handled as input VAT where
the entity is VAT-registered.

## Changes (exact)

### A. Facts + constructor

1. `api/src/constructors/facts.py`: new `CardPurchaseFacts` model —
   `purchase_date: date`, `bank_account_code: str` (4-digit),
   `account_code: str` (4-digit — the expense or asset account hit),
   `net_pence: int` (>0), `vat_rate: Optional[str]` (one of "20%", "5%",
   "0%", "exempt", or absent), `description: str` (default ""),
   `supplier_name: Optional[str]` (optional counterparty for the contact
   auto-creation).  Register in FACTS_MODELS.
2. `api/src/constructors/entry_constructors.py`: new `card_purchase`
   constructor, mirroring `supplier_invoice`'s VAT helpers
   (`_vat_for_line` / `_resolve` / `_check_balance` / `_finish`):
   - legs: Dr `account_code` net; if vat_rate ∈ {"20%", "5%"} Dr 2220
     Input VAT (the vat_amount); Cr `bank_account_code` gross.  "0%" /
     "exempt" / None → no VAT legs.
   - account_code must resolve to an Expense or Asset account (validate
     via the COA like the other constructors; wrong class →
     `ConstructorError`).
   - descriptions: purchase line + "Input VAT 20%" style + "Card payment".
3. `api/src/constructors/__init__.py`: register `"card-purchase"` in
   CONSTRUCTORS.

### B. Catalog + prompt

4. `skills/catalog/transactions/card-purchase/SKILL.md` — new SKILL.md
   modeled on supplier-invoice (frontmatter: skill_id, title, framework
   uk-gaap/ifrs/us-gaap, exact fact schema, legs table, UK GAAP treatment
   note: asset purchases capitalised net of recoverable input VAT; expense
   purchases to P&L net).
5. `skills/registry.yaml`: register card-purchase (mirror the
   supplier-invoice entry shape).
6. `api/src/services/orchestrator.py`: add `card-purchase` to
   `SKILL_DESCRIPTIONS` ("Purchase paid immediately by card or bank
   transfer (expense or asset)") AND to `CONTACT_NAME_FACT_FIELDS`
   (`("supplier_name",)`) + `CONTACT_TYPE_BY_SKILL` (`"supplier"`) so a
   named supplier auto-creates a supplier contact (F29 pattern).

### C. Tests

7. Unit (`api/tests/unit/test_constructors.py` or wherever supplier_invoice
   constructor tests live): expense+VAT 20%, asset+VAT, exempt (no VAT
   legs), 0% (no VAT legs), gross = net + vat exact pence, wrong account
   class → ConstructorError, unknown code → error, balance identity.
8. Orchestrator unit: card-purchase message → preview legs + supplier
   contact auto-created; prompt contains the skill (Today-is test pattern).
9. e2e (test_orchestrator_e2e.py): card-purchase → preview → approve →
   posted + parity ok.
10. Full suite: api ≥ 1361, chat-ui 46, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/constructors/facts.py`,
  `api/src/constructors/entry_constructors.py`,
  `api/src/constructors/__init__.py`, `api/src/services/orchestrator.py`
  (SKILL_DESCRIPTIONS, the two CONTACT maps, and `_DATE_FACT_FIELDS` —
  purchase_date must flow to the draft's effective_date), the new
  SKILL.md, `skills/catalog/registry.yaml` (NOT `skills/registry.yaml`,
  which is the MCP tool registry — see the generator's deviation note),
  the test files, this contract.
- NOT: chat-ui, routes, transaction/contact services, report engine,
  formance-mcp, other skills.

## Verifier fix-list (applied)

- The 2220 Input VAT account is resolved ONLY when a VAT leg is actually
  produced (exempt/0%/no-rate purchases work on COAs without an input-VAT
  account — 6 of the 8 shipped templates; the eager resolve broke them).
- Added the bank-account wrong-class test.

## Success criteria

1. Suites green (above).
2. Live check after deploy: "I ordered a MacBookPro for £2500 paid with
   the company debit card from the Revolut GBP account" → preview with
   Dr asset-or-expense / Dr 2220 Input VAT / Cr bank (mentor may ask
   whether it's an asset or expense — that is acceptable; a clean
   clarification beats a wrong capitalisation).
