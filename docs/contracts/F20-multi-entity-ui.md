# CONTRACT F20 — Multi-Entity UI + Cross-Check Scoping + F19 Residuals

## Goal

Finish multi-entity: the user can switch entity in the UI (all
orchestrator/report/pending calls carry the entity), the Postgres
cross-check is entity-scoped (no more diagnostic false drift with two
entities), and the F19 residuals are closed (reject ownership, unknown
entity_id validation, dead-import cleanup).

## Changes (exact)

1. **Report cross-check scoping** (`report_service.py`,
   `validators/report.py` where needed):
   - `ReportService.run_pl/run_bs/run_tb` gain optional
     `entity_id: Optional[uuid.UUID] = None`; their Posting/Transaction
     queries filter `entity_id` when provided (None = legacy unfiltered).
     ReportRunRequest gains an optional `entity_id` field (validator).
   - `formance_reports.verify_against_postgres(db, report_dict, report_type,
     entity_id=None)` passes the scope through. The F10 report route
     (already entity-aware) passes its resolved entity_id.
2. **Reject ownership** (`orchestrator.py` + `routes/orchestrator.py`):
   `reject_entry(db, transaction_id, entity_id=None)` — when entity_id is
   set, refuse (404 EntityNotFoundError-style or 422) a draft stamped with
   a DIFFERENT entity; the route passes its resolved scope through.
3. **Unknown entity_id validation**: the orchestrator + report routes'
   `_resolve_scope` validates an explicit entity_id (exists + active) →
   404 `{type:'entity_not_found'}` instead of silent `main` degradation.
   Apply the same in `get_ledger_for_entity`'s callers where feasible
   (orchestrator approve/message, report routes, status endpoint already
   loads the entity itself).
4. **Dead-import cleanup**: remove the `# noqa: F401 — patch seam for
   legacy tests` import of `get_active_ledger` from `orchestrator.py` if
   the F19-updated tests no longer patch it there (verify and update the
   patch targets if needed); `get_active_ledger` itself stays in
   `entity_service.py` (public fail-loud API).
5. **chat-ui entity switcher**:
   - `chat-ui/server.py`: add `GET /api/entities` proxy (→
     `/api/v1/entities`) — existing `_upstream_client` seam, 502 on
     failure, no mock.
   - `chat-ui/index.html`: an entity dropdown (id `#entity-select`) fed
     from `/api/entities`; selection stored in AppState (default = first
     entity); ALL orchestrator calls (message/approve/reject/pending) and
     report fetches append `?entity_id=<selected>` when an entity is
     selected; the reports pane + preview pane re-fetch on switch. New
     stable DOM ids: `#entity-select`, `#entity-label`.
   - `api/tests/test_chatui_contract.py`: assert the new ids + the
     entity_id query wiring string (`entity_id=`) exists in the JS.
   - `chat-ui/tests/test_server.py`: forwarding tests for `/api/entities`
     (path/status/502, no mock).

## Backward compat

- entity_id absent everywhere → legacy NULL behavior, byte-identical.
- Single entity → auto-selected in the UI (existing flows unchanged).

## Tests

- Unit: run_pl/bs/tb entity filter (statement-capture or scoped-fixture);
  verify_against_postgres passes scope; reject_entry ownership; _resolve_scope
  404 on unknown id.
- e2e (extend `test_multi_entity_e2e.py`): two entities both with posted
  data → report route for A with cross_check diffs EMPTY (previously false
  drift — the F19 verifier's measured case); reject with mismatched
  entity_id refused; unknown entity_id → 404.
- Full suite:
  `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
  → 0 failed, ≥ 1251; chat-ui suite ≥ 14+new; formance-mcp 60 unchanged.

## Boundaries

- ONLY: report_service + validators/report (entity_id additions),
  formance_reports (verify_against_postgres scope), orchestrator + its
  routes (reject ownership + scope validation), entity_service (if needed
  for scope validation helper), chat-ui (server + index.html + tests),
  test_chatui_contract, new/extended tests, this contract.
- NOT: models/migrations, constructors, catalog, ledger_parity, Formance
  MCP, api clients.

## Success criteria

1. Suites green (api ≥ 1251 + new, chat-ui ≥ 14 + new, mcp 60).
2. `git status --porcelain -uall` = allowed paths only.
3. Verifier probes: F19's measured false-drift case now returns empty
   cross_check diffs for entity A; unknown entity_id → 404; reject
   cross-entity refused; UI contract ids + entity_id query wiring present.
