# CONTRACT F19 — Multi-Entity Orchestrator, Reports & Parity Scoping

## Goal

Complete multi-entity operation: the orchestrator, parity verifier, and
report engine all resolve the ENTITY's scope and ledger — closing the F18
verifier's I2 (approve/report silently post to `main` with 2+ entities),
I3 (postings can reference another entity's accounts), and I5 (parity
code-map collapse). Fail-loud replaces fail-silent: with 2+ active
entities and no explicit entity_id, ambiguous requests raise
`AmbiguousEntityError` 409.

## Changes (exact)

1. **Fail-loud entity resolution** (`entity_service.py`):
   - `get_active_ledger` no longer returns `"main"` for 2+ active entities
     — it raises `AmbiguousEntityError` (delegates to `get_active_entity`).
     This is a DELIBERATE behavior change; the F12 unit test
     `test_get_active_ledger_multiple_entities_returns_main` is UPDATED to
     assert the raise (sanctioned test change — its premise predates
     multi-entity).
2. **Orchestrator** (`orchestrator.py`):
   - `orchestrate_message(db, message, history, llm, entity_id=None)`:
     entity scope resolved as `entity_id or (await get_active_entity(db)).id
     or None`; COA resolution (`resolve_from_coa`) and account-code
     resolution are ENTITY-SCOPED; the draft is stamped with the entity_id.
   - `approve_entry(db, transaction_id, ledger=None)`: ledger resolution
     uses the transaction's own entity (`get_ledger_for_entity(tx.entity_id)`)
     when `ledger` is None — NEVER `get_active_ledger`; the post-approve
     `verify_parity` runs on that same ledger AND with the tx's entity_id
     scope.
   - `reject_entry` unchanged (draft scoping comes free via the stamped
     entity_id).
3. **Parity** (`ledger_parity.py`): `verify_parity(db, ledger="main",
   entity_id: Optional[uuid.UUID] = None)` — the Postgres universe
   (transactions + accounts, code_to_account map) is filtered by entity_id
   when provided (NULL-scope: unfiltered legacy behavior, unchanged).
   Callers in F19 pass the entity scope.
4. **TransactionService** (`transaction_service.py`):
   `_validate_accounts` gains an entity-ownership check: when the
   transaction carries entity_id, every posting's account must belong to
   the same entity (or be NULL-scoped legacy) — else
   `EntityMismatchError` (422). This closes I3 at the write path.
5. **Report engine + routes**:
   - `formance_reports.py` `generate_*` gain `entity_id: Optional[uuid.UUID]
     = None`; the COA classification (build_code_class_map input) is
     entity-scoped when provided; ledger param already exists.
   - `routes/formance_reports.py`: resolve entity scope
     (`entity_id or active-entity or None`, ambiguous → 409) and pass both
     `ledger` (entity's) and `entity_id` to the engine.
   - `routes/entities.py` status endpoint: parity call gains the entity's
     scope (`verify_parity(db, ledger=entity.ledger_name,
     entity_id=entity.id)`).
6. **Orchestrator routes** (`routes/orchestrator.py`): optional `entity_id`
   on message/approve/reject/pending; resolve the scope (explicit >
   single-active > NULL legacy; ambiguous → 409); pass it through;
   `pending` lists drafts scoped to the entity (stamped rows).

## Backward compat

- No entity present → resolution is NULL → legacy unfiltered behavior +
  `main` ledger, byte-identical to today. All existing tests stay green
  EXCEPT the one sanctioned get_active_ledger test update.
- One active entity → its scope/ledger (the F11/F12 flows keep working).

## Tests

- Unit (`test_entity_service.py` update + new `test_multi_entity_scope.py`):
  get_active_ledger raises on 2+; orchestrator resolves entity scope and
  stamps drafts; approve_entry uses tx-entity ledger + scoped parity
  (mock asserts); verify_parity entity_id filtering (statement-capture);
  _validate_accounts EntityMismatchError; routes 409 on ambiguity.
- e2e (extend `test_multi_entity_e2e.py`): two entities loaded; A's draft
  approved → recorder captures A's ledger + parity ok on A's scope; B's
  pending list excludes A's drafts; report generation for A uses A's COA
  classification (no code collapse); hostile cross-entity posting
  (A-tx with B's account) → EntityMismatchError; ambiguous route call with
  no entity_id → 409.
- Full suite:
  `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
  → 0 failed, 0 errors, ≥ 1216.

## Boundaries

- ONLY: entity_service, orchestrator, ledger_parity, transaction_service
  (_validate_accounts + EntityMismatchError), formance_reports, the three
  route files (orchestrator, formance_reports, entities), the sanctioned
  get_active_ledger test update, new/extended tests, this contract.
- NOT: models/migrations (F18 done), constructors, catalog, chat-ui
  (F20), Formance MCP, clients.

## Success criteria

1. Suite ≥ 1216 + new tests green; chat-ui 14, formance-mcp 60 unchanged.
2. `git status --porcelain -uall` = allowed paths only.
3. Verifier probes: I2 closed (two entities, approve → correct per-entity
   ledger, ambiguous route → 409, NO silent 'main'); I3 closed
   (cross-entity posting refused); I5 closed (per-entity parity ok with
   two COAs loaded, no false drift).
