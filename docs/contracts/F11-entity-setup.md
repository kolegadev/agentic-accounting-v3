# CONTRACT F11 — Entity Setup & Admin + Chart of Accounts (initial ledger)

## Goal

The onboarding flow the thesis needs before any transaction can be recorded:
create an accounting **entity** (company) with framework/fiscal/VAT settings
and an **admin owner**, load its **chart of accounts**, create its
**Formance ledger**, and post **opening balances** (as an approvable draft)
— the "setting up the initial ledger for the company" step.

## Deliverables

1. **`api/src/models/entity.py`** — `Entity` model:
   `id` (uuid pk), `name` (required), `legal_type`
   (enum: sole_trader|limited_company|partnership|micro_entity|
   property_landlord|other), `framework` (enum: uk-gaap|ifrs),
   `fiscal_year_start` (str "MM-DD", default "04-06"), `vat_registered`
   (bool), `vat_number` (nullable), `vat_scheme` (enum
   standard|cash|flat_rate, nullable), `currency` (default "GBP"),
   `ledger_name` (str, default `entity-<uuid4hex[:8]>`), `created_by`
   (FK users.id — the admin owner), `is_active`, `created_at`,
   `updated_at`.
   Plus alembic migration **`api/alembic/versions/015_entities.py`**
   (follow the existing migration style — read 013/014 for the pattern).

2. **`api/src/services/entity_service.py`** — create_entity,
   get_entity, list_entities, update_entity; validation errors as
   `EntityServiceError(status_code, message)` (name required, unknown id,
   duplicate name → 409).

3. **`api/src/services/setup_service.py`** — the initial-ledger setup:
   - `setup_entity(db, entity_id, template_name, bank_accounts=None,
     opening_balances=None) -> SetupResult`
   - GUARD: refuses if accounts already exist for the entity (COA loaded →
     `SetupError` "already set up"; idempotency = refuse, never duplicate).
   - Loads the COA template via the existing `CoaService.load_template`
     (fuzzy *_vat/*_no_vat match).
   - Creates bank accounts via the existing `BankAccount` model / bank
     service (name, sort_code, account_number, opening_balance).
   - Ensures the Formance ledger exists via a NEW minimal method on the
     existing client: `formance_client.create_ledger(ledger_name)` — POST
     `{base}/v2/{ledger}` (check the actual Formance v2 route in the F5
     service client code and the Formance docs in repo; tolerate 409
     already-exists and connection failure with a logged warning, setup
     continues — Formance may be started later).
   - Opening balances: build the F4 **opening-balances** constructor facts
     (`assets`/`liabilities` items with account codes + balance_pence,
     `equity_pence`, `as_at`) from the request, run the constructor,
     resolve codes via CoaService, create a **DRAFT** transaction via
     `TransactionService.create_transaction` (never post here), return the
     transaction ids. Approval happens through the existing orchestrator
     approve route — drafts are the preview/approval mechanism.
   - `SetupResult`: entity_id, coa_count, bank_account_ids, ledger_name,
     opening_transaction_ids, opening_draft_total_pence.
4. **`api/src/routes/entities.py`** — registered in `index.py`:
   - `POST /api/v1/entities` — auth required (`get_current_user`); creates
     the entity (created_by = current user) and, if `setup` body fields are
     provided, runs `setup_entity` immediately. 201 with entity + setup
     result.
   - `GET /api/v1/entities`, `GET /api/v1/entities/{id}`
   - `POST /api/v1/entities/{id}/setup` — `require_role("owner","admin")`;
     body {template_name, bank_accounts?, opening_balances?} → 200
     SetupResult; SetupError → 409; EntityServiceError → 404.
   - `GET /api/v1/entities/{id}/status` — {entity, has_coa, account_count,
     bank_accounts, pending_opening_transactions (drafts), ledger_name,
     formance: reachable|unreachable|not_configured, parity: ok|drift|na}
     (parity via `verify_parity` when Formance reachable).
5. **Tests**
   - `api/tests/unit/test_entity_service.py` + `test_setup_service.py` —
     mocked DB/formance: entity validation/errors, ledger_name derivation,
     setup guard (double setup refused), create_ledger called, opening
     balance facts → correct DRAFT legs (e.g. Dr 1000 bank / Cr 3000
     capital, exact pence, balanced), draft NOT posted by setup,
     create_ledger failure tolerated.
   - `api/tests/integration/test_entity_setup_e2e.py` — REAL aiosqlite ORM
     (existing SQLite-schema pattern) + scripted fake Formance recorder:
     create entity → setup (uk_limited_company_vat + bank 1000 + opening
     balances £10,000 capital) → COA loaded (count > 0), draft legs
     correct → approve via `TransactionService.post_transaction` (mirror
     recorded) → `verify_parity` ok=True → status endpoint data reflects
     posted + parity.
   - `api/tests/test_entity_routes.py` — TestClient with mocked services:
     auth required, role guard, 201/200/404/409 mappings.
6. Full suite:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 1069.

## Boundaries

- ADDITIVE: new model + migration + services + routes + tests + the ONE
  minimal `formance_client.create_ledger` method + this contract.
- NO changes to existing service behaviour, constructors, catalog,
  orchestrator, F1/F2 modules.

## Accounting spot-checks (verifier)

- Opening-balance draft: assets debited, equity/liabilities credited,
  sums equal; never auto-posted.
- Setup is idempotency-refusing (never double-loads a COA).
- created_by == the authenticated admin user; role guard on setup.
