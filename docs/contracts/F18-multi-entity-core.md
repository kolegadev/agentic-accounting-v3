# CONTRACT F18 — Multi-Entity Core (schema + scoping + per-entity ledgers)

## Goal

The foundation for multiple accounting entities in one database: every
entity gets its OWN chart of accounts, bank accounts, transactions, and
Formance ledger — with total isolation. Backward compatibility is the hard
requirement: with no entity (or a single legacy NULL-scoped dataset), the
system behaves EXACTLY as today and all 1175 existing tests stay green.

## Design (the contract)

1. **Migration `017_entity_scoping.py`** (down_revision '016'):
   `entity_id UUID NULL REFERENCES entities(id)` added to `accounts`,
   `bank_accounts`, `transactions` (+ indexes on entity_id for each).
   NULL = the legacy/global scope. NO data migration needed (existing rows
   stay NULL = legacy).
2. **Models** (`api/src/models/account.py`, `bank_account.py`,
   `transaction.py`): `entity_id: Mapped[Optional[uuid.UUID]]` matching the
   migration.
3. **Entity resolution** (`api/src/services/entity_service.py`):
   - `get_active_entity(db) -> Optional[Entity]`: exactly one active entity
     → it; zero → None (legacy global mode); MORE than one → raises
     `AmbiguousEntityError` (EntityServiceError 409) — an explicit
     entity_id is then required.
   - `get_ledger_for_entity(db, entity_id) -> str`: entity's ledger_name,
     else `"main"` (None/unknown → legacy).
   - `get_active_ledger` KEEPS its current single-entity behavior
     (delegates to get_active_entity; >1 → raises — its callers will be
     made entity-aware in F19; F18 must not change its single-entity
     semantics).
4. **CoaService** — optional `entity_id: Optional[uuid.UUID] = None` on:
   `load_template` (stamps created accounts), `list_accounts` (filter),
   `get_account_by_code` (filter), `create_account` (stamp). Default None =
   legacy behavior (no filter) — so every existing caller/tests unchanged.
5. **TransactionService** — `create_transaction` accepts
   `entity_id: Optional[uuid.UUID] = None` (stored on the row);
   `list_transactions` gains optional `entity_id` filter;
   `post_transaction`/`reverse_transaction` resolve the mirror ledger from
   the TRANSACTION's own entity_id via `get_ledger_for_entity` (replacing
   the `ledger or "main"` default) — the ledger always matches the row's
   entity, never a global guess.
6. **BankAccount creation** (bank_service.create_account or the setup
   path): optional entity_id stamped; list filter optional entity_id.
7. **setup_service** — entity-scoped guards/skips: account/bank/draft
   counts filtered by the entity's entity_id; COA template accounts,
   banks, and opening-balance drafts all stamped with the entity's id.
   Legacy (entity_id=None) keeps the global semantics EXACTLY (counts
   unfiltered) so F11/F12 tests stay green.
8. **Backward compat proof**: a dedicated test that runs the F11/F12
   single-entity e2e flow with entity_id=None on every call and asserts
   identical behavior; plus a multi-entity e2e: two entities → two COAs
   (same template), two ledgers, isolated transactions/opening balances;
   cross-entity access must be impossible (get_account_by_code scoped,
   list_transactions scoped); each entity's mirror goes to its own ledger
   (fake Formance recorder captures ledger names per entity).

## Boundaries

- ALLOWED: the 3 models, migration 017, coa_service.py,
  transaction_service.py, bank_service.py (only the scoping additions),
  entity_service.py, setup_service.py, SQLite test DDL updates
  (integration test fallback schemas gain the entity_id columns), new
  tests, this contract.
- NOT allowed: orchestrator/report engine/parity/routes/UI changes (F19/F20),
  constructors, catalog, clients, Formance MCP.

## Success criteria

1. Full suite:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, ≥ 1175 (all existing tests green + new
   multi-entity tests).
2. chat-ui 14, formance-mcp 60 unchanged.
3. `git status --porcelain -uall` = allowed paths only.
4. Verifier confirms: backward compat (NULL-scope == old behavior on the
   F11/F12 flows), isolation (two entities can't see each other's rows or
   ledgers), and per-transaction ledger resolution.
