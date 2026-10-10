# CONTRACT F12 — Entity-Ledger Wiring + Setup Hardening

## Goal

Close the four F11 verifier gaps: (1) setup error mapping (template not found
→ 404, duplicate ledger_name → 409 instead of 500s), (2) setup resumability
after partial failure (no more permanent 409 lock-out), (3) the entity's own
Formance ledger is actually USED for posting, parity, and reports (defaults
to `main` only when no entity exists — backward compatible).

## Part A — Error mapping (`api/src/routes/entities.py`, `entity_service.py`)

1. `_run_setup` catches `CoaServiceError` (and its `TemplateNotFoundError`
   subclass) → 404 `{type:"coa_error", message}`; `SetupError` → 409;
   `EntityServiceError` → 404 (as today). No uncaught escape.
2. Entity create: duplicate `ledger_name` → catch `IntegrityError` →
   `EntityServiceError` 409 `{type:"duplicate_ledger_name"}`; when the name
   is AUTO-DERIVED and collides, regenerate `entity-<uuid4hex[:8]>` and
   retry up to 3 times before failing.

## Part B — Setup resumability (`setup_state`)

3. Migration **`api/alembic/versions/016_entity_setup_state.py`**:
   `setup_state` VARCHAR(16) NOT NULL DEFAULT 'none' + CHECK
   (none|in_progress|complete) on `entities`; down_revision = '015'.
4. `setup_service.setup_entity` lifecycle:
   - Refuse with `SetupError` 409 ONLY when `setup_state == 'complete'`.
   - On entry (state none|in_progress): set `setup_state='in_progress'` and
     commit BEFORE any heavy step.
   - Steps skip cleanly on resume (no duplicates):
     - COA: skip `load_template` if `account_count > 0`.
     - Bank accounts: skip if any bank account already exists.
     - Opening balances: skip if a draft transaction already exists
       (any row with status='draft').
     - `create_ledger` always called (idempotent on Formance side).
   - On success: `setup_state='complete'` + commit.
   - On failure: state remains `in_progress` → a re-run RESUMES instead of
     being refused. Never delete partial data.
   - Existing F11 unit tests that asserted the old global-guard semantics
     may be updated to the new semantics (tests only).

## Part C — Entity-ledger wiring

5. `entity_service.get_active_ledger(db) -> str` — returns the single active
   entity's `ledger_name`, else `"main"` (no entity / inactive only).
6. `transaction_service.post_transaction(db, transaction_id,
   ledger: str | None = None)` and `reverse_transaction(...)` — pass
   `ledger or "main"` through to `_post_to_formance`. Backward compatible:
   existing callers/tests keep working with the default.
7. `orchestrator.approve_entry(db, transaction_id, ledger=None)` — resolves
   `ledger = ledger or await get_active_ledger(db)`; uses it for
   `post_transaction` AND the post-approve `verify_parity(db, ledger)`.
8. `routes/formance_reports.py` — resolve `get_active_ledger(db)` and pass
   it to every `generate_*` call (engine signatures already accept a ledger
   arg — verify and pass positionally/keyword correctly).
9. `routes/entities.py` status endpoint — `verify_parity(db,
   ledger=entity.ledger_name)`.

## Tests

- Route/error: template not found → 404 (not 500); duplicate ledger_name →
  409; auto-derived collision retry (mock IntegrityError once).
- Resumability: full setup → state complete → second call refused (409);
  simulated mid-failure (monkeypatch BankService.create_account to raise
  once) → state in_progress + COA loaded → re-run resumes (no duplicate
  accounts/banks, opening draft created once, state complete).
- Ledger wiring (unit): post_transaction passes ledger to
  `_post_to_formance` (mock recorder captures ledger name); approve_entry
  resolves entity ledger for post + parity; report routes call `generate_*`
  with the entity ledger; status parity uses entity ledger; NO entity →
  everything uses `"main"` (backward compat).
- e2e (real aiosqlite ORM + fake Formance recorder): create + setup entity →
  approve opening draft → recorder captured ledger == `entity-<hex>` →
  verify_parity on that ledger ok=True → report route with entity passes
  entity ledger to the engine.
- Full suite:
  `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
  → 0 failed, 0 errors, count ≥ 1102.

## Boundaries

- ALLOWED modifications: `api/src/models/entity.py` (+state field, no
  behaviour change elsewhere), alembic 016, `setup_service.py`,
  `entity_service.py`, `routes/entities.py`, `routes/formance_reports.py`,
  `transaction_service.py` (signature + pass-through ONLY — pairing/Decimal
  logic untouched), `orchestrator.py` (approve_entry ledger resolution
  only), F11's tests (new-semantics updates), new tests, this contract.
- NOT allowed: constructors, catalog, F9 engine internals, F2 verify_parity
  internals, F1 build_formance_postings, chat-ui, gateway, Formance MCP.
