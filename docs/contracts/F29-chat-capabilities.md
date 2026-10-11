# CONTRACT F29 — Chat Capabilities: Contact Auto-Creation + Query Mode

## Goal

Close two manual-test gaps: (1) invoicing a NEW customer must create the
contact automatically (the v2 pipeline did this; the F6 rebuild dropped
it), so the customer appears in the Contacts panel and on the draft; (2)
the chat must answer account QUERIES (balances, trial balance, account
list, contacts, pending drafts) — not just process transactions — with
DETERMINISTIC data (the LLM classifies the query; it never computes the
numbers).

## Changes (exact)

### A. Contact auto-creation (`api/src/services/orchestrator.py`)

1. In `orchestrate_message`, after the constructor builds the entry (step
   3/4), derive the contact identity from the facts when the skill has
   one:
   - `supplier-invoice`, `supplier-payment` → `facts.supplier_name`
   - `sales-invoice`, `customer-receipt` → the customer-name fact field
     (READ the F4 facts models to find the exact field name —
     `customer_name` or similar; also `sales-invoice` may carry
     `customer_name`).
   - Any email/vat fields on the facts pass through to find_or_create.
2. Call `ContactService.find_or_create(db, name, email=..., vat_number=...)`
   → stamp `contact_id` on the `TransactionCreate` (the validator already
   has the field) → the draft carries the contact; include the contact
   name in the preview result (`preview.contact` = {id, name, created}).
3. find_or_create failures degrade to the draft WITHOUT a contact (logged
   warning) — never block the transaction on contact creation.

### B. Query mode (`api/src/services/orchestrator.py`)

4. `build_system_prompt` gains query skills + rules: the LLM may return
   `{"skill_id": "query", "facts": {"query_type": "<one of>",
   "account_code": "<4-digit code, optional>"}}` where query_type ∈
   {trial_balance, account_balance, accounts, contacts, pending}. Rule:
   if the user asks a QUESTION about their books (not a transaction),
   choose query with the right query_type. Transaction skills unchanged.
5. `orchestrate_message` handles `skill_id == "query"` BEFORE the
   constructor path: parse query facts (pydantic `QueryFacts` — new small
   model in `api/src/constructors/facts.py` or orchestrator-local),
   resolve the entity scope (same `resolve_entity_scope` + COA load as the
   transaction path), then run the DETERMINISTIC handler:
   - `trial_balance` → `generate_tb(db, ledger, as_at=today, entity_id)`
     → return {status:"answer", query_type, data:{trial_balance: rows,
     total_debits, total_credits}}
   - `account_balance` → same TB, filtered to `account_code` → one row
     (unknown code → structured error 404-style `account_not_found`)
   - `accounts` → `CoaService.list_accounts(db, entity_id=scope_id)`
     summarized (code, name, category, type)
   - `contacts` → `ContactService.list_contacts(db)` (global)
   - `pending` → `TransactionService.list_transactions(db,
     status="draft", entity_id=scope_id)` summarized
   - unknown query_type → structured `invalid_query` error
   All amounts stay integer pence in the API payload (the UI formats).
   NO LLM narration of numbers — the answer is the deterministic data.
6. `OrchestrateResult` gains the answer shape (status "answer" + data).

### C. UI (`chat-ui/index.html`)

7. `sendMessage()` already posts to the orchestrator; handle the new
   "answer" response: render an assistant chat message (e.g. "Here's your
   trial balance") + a TABLE of the data (reuse `esc()`/`renderTable`
   with `fmtMoney` for pence). No new fetch needed (same endpoint).
8. The preview pane is untouched for answers (no draft). Contact name from
   the preview shows in the preview header when present.
9. Contacts panel refreshes after a message that creates a contact (call
   the existing contacts loader on preview success — add it beside the
   pending/books refreshes where relevant: after message preview, not just
   approve).

### D. Tests

10. Unit (`test_orchestrator.py` + new `test_orchestrator_query.py`):
    contact find_or_create called with the facts' name for
    supplier-invoice + customer-receipt; contact_id stamped; created flag
    flows to preview.contact; find_or_create failure → draft still created
    without contact (warning). Query mode: each query_type returns the
    expected data with mocked services; unknown type → invalid_query;
    transaction messages still route to constructors; the LLM prompt
    contains the query instructions (Today-is test pattern).
11. e2e (`test_orchestrator_e2e.py` extension): a sales-invoice to a NEW
    customer via orchestrate_message → contact appears via
    ContactService.list_contacts; approve → posted; a query message
    (fake LLM returning query) → answer data matches the real ledger.
12. Full suite: api ≥ 1319, chat-ui ≥ 40, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `orchestrator.py`, `constructors/facts.py` (QueryFacts model),
  `chat-ui/index.html`, `routes/orchestrator.py` (the 6-line passthrough:
  answer branch + `"contact"` in the preview dict — see Implementation
  notes), the two test files, this contract.
- NOT: constructors' entry logic, catalog, report engine, parity,
  transaction_service, other routes, Formance MCP.

## Success criteria

1. Suites green (above).
2. Live check after deploy: an invoice to a new customer creates the
   contact (visible in Contacts); asking "what's my trial balance" in the
   chat returns the real TB table; balances answer per-account.

## Implementation notes (added by the F29 implementer)

- The original Boundaries note ("verify no route change is needed") was
  WRONG: `POST /api/v1/orchestrator/message` builds its response dict by
  hand.  The route IS patched now (per the amended ALLOWED boundary):
  an `answer` branch returns `200 {status, query_type, data, contact}`
  and `"contact": result.contact` is forwarded inside the preview dict.
  Both covered by route tests.
- Query facts are validated against `QUERY_TYPES` by the orchestrator
  (not by a `Literal` in `QueryFacts`) so an unknown `query_type` surfaces
  as the structured `invalid_query` error, never as `invalid_facts`.
- Contact resolution runs after the constructor/legs and BEFORE
  `create_transaction`; on failure it logs a warning, rolls the session
  back (a failed statement aborts a Postgres transaction) and the draft is
  created without a contact.
- Independent-verifier fix-list (applied): auto-created contacts carry the
  right type per skill family (customer for sales-invoice/customer-receipt
  via `CONTACT_TYPE_BY_SKILL`; `ContactService.find_or_create` gained a
  `type` parameter, default "supplier"); `account_balance` falls back to
  the COA (zero row) when the code has no postings yet instead of a false
  `account_not_found`; the answer title renders (separate
  renderMarkdown + renderTable passes); only 4-digit-code cells are
  clickable in tables; contacts/pending answers show "Showing N of M"
  when the 50-row default truncates; route tests for the answer branch and
  preview.contact passthrough added.
