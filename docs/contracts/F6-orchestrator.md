# CONTRACT F6 — Orchestrator (LLM classify → constructor → preview → approve → post)

## Goal

The production pipeline that makes the thesis real: the LLM ONLY classifies
the transaction type and extracts facts; the F4 deterministic constructor
builds the entry; the user previews and approves; only then does the entry
post and mirror to Formance — followed by an F2 parity check. The LLM has no
path to construct money math.

## Pipeline

```
user message
  -> LLM extract: {skill_id, facts}          (classification + extraction ONLY)
  -> parse_facts (pydantic, F4)              (invalid -> structured error)
  -> constructor = CONSTRUCTORS[skill_id]    (F4; ConstructorError -> error)
  -> draft transaction via TransactionService.create_transaction (v2 draft state)
  -> PREVIEW returned: legs, amounts, skill_id, transaction_id, total
  [user approves]
  -> TransactionService.post_transaction     (assigns JE ref, mirrors Formance via F1)
  -> verify_parity (F2)                      (posting must not drift the ledgers)
  -> POSTED result
  [user rejects]
  -> TransactionService.discard_draft        (draft only; posted can never be discarded)
```

## Deliverables

1. `api/src/services/orchestrator.py` — the pipeline above:
   - `orchestrate_message(db, message, history, llm) -> OrchestrateResult`
     (dataclass: `status` in {"preview", "error"}, preview carries
     transaction_id, skill_id, legs, total_pence, formance_preview;
     error carries structured message — never a raw traceback)
   - `approve_entry(db, transaction_id) -> PostResult` (posts, mirrors,
     runs verify_parity, returns posted tx + parity report)
   - `reject_entry(db, transaction_id) -> RejectResult` (discards draft)
   - LLM prompt builder: system prompt lists all 10 skills with their Facts
     JSON schemas (derived from the F4 pydantic models via
     `.model_json_schema()`), plus rules (money in integer pence, dates
     ISO, vat_rate enum, choose the closest skill or return
     {"skill_id": null, "clarification": "..."}). Temperature 0.
   - LLM transport: httpx to an OpenAI-compatible endpoint, env
     `LLM_API_URL`/`LLM_API_KEY`/`LLM_MODEL` (same convention as v2's
     llm_router). LLM unreachable -> structured error.
2. ONE minimal production addition (the only non-additive change allowed):
   `TransactionService.discard_draft(db, transaction_id)` in
   `api/src/services/transaction_service.py` — deletes ONLY transactions
   with status `draft`; raises `TransactionNotDraftError` otherwise. Add its
   unit tests.
3. Tests:
   - `api/tests/unit/test_orchestrator.py` — with mocked LLM transport +
     mocked DB (AsyncMock pattern): happy path per representative skill
     (supplier-invoice with VAT, bank-transfer, payroll); invalid facts ->
     structured error; unknown skill id -> error; LLM unreachable -> error;
     clarification response -> error/preview-absent; approve posts + mirrors
     (mock formance client) + parity called; approve a non-draft ->
     TransactionNotDraftError surfaced; reject discards draft; reject a
     posted tx -> error (never deleted).
   - `api/tests/integration/test_orchestrator_e2e.py` — REAL sqlite+aiosqlite
     ORM (pattern from test_transaction_orm_reversal.py), real constructors,
     real TransactionService, fake LLM returning canned extraction JSON,
     scripted fake Formance mirror recorder: full round-trip for 2 messages
     (e.g. supplier invoice VAT + customer receipt) — preview shows correct
     legs, approve -> posted + mirrored, verify_parity reports ok=True;
     reject path leaves no posted rows and parity stays ok=True; one
     sabotage (drop a mirror) -> parity reports the drift.
4. Full suite green:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 933.

## Hard rules

- The LLM's output is ONLY `{skill_id, facts}` — no posting, no amounts
  beyond facts fields, no account codes (account resolution is the
  constructor's job via the COA resolver).
- Nothing posts without `approve_entry`. No code path may post inside
  `orchestrate_message`.
- All money stays integer pence until F1 converts to Decimal strings at the
  mirror boundary.
- Drafts are the preview mechanism; posted/reversed rows are append-only
  (never deleted) — `discard_draft` is draft-only.

## Boundaries

- Additive: `api/src/services/orchestrator.py` + the two test files +
  `TransactionService.discard_draft` (+ its unit tests) + this contract.
- No changes to routes, chat_service, gateway, chat-ui, constructors, F1/F2
  modules beyond the allowed addition.
