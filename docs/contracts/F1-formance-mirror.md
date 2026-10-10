# CONTRACT F1 — Correct Double-Entry Formance Mirroring

## Goal

Every posted and reversed journal entry is mirrored to the Formance ledger as
paired (source, destination) postings, making Formance a faithful double-entry
image of the Postgres ledger — no `world` contra account.

## Boundaries

- MUST NOT change: Postgres schema/models, validators, report engine, public
  API routes/contracts, tool registry, chat pipeline, MCP gateway.
- MAY change: `api/src/services/transaction_service.py` (`_post_to_formance`
  and `reverse_transaction`), `api/src/services/formance_client.py` (amount
  serialization only), tests under `api/tests/unit/` and
  `api/tests/integration/` that cover these.

## Success Criteria (all testable)

1. **Pairing:** a balanced 2-leg entry [(Dr A, £1.00), (Cr B, £1.00)] emits
   exactly ONE Formance posting: `{source: B.code, destination: A.code,
   amount: "1.00", asset: "GBP/2"}`. The string `"world"` never appears in
   source or destination.
2. **Compound entries:** a 3-leg entry [(Dr A £1.00), (Dr B £0.50),
   (Cr C £1.50)] emits TWO postings: `{C→A, 1.00}` and `{C→B, 0.50}`.
   General rule: greedy amount-aware pairing of debit legs to credit legs in
   posting order; each Formance posting's source is the credit account and
   destination is the debit account.
3. **Reversals:** `reverse_transaction()` posts the reversing entry to Formance
   with the same correct pairing (swapped sides produce source/destination
   swapped correctly).
4. **Exact money:** amounts convert from integer pence to major units using
   `Decimal` (e.g. 5000 → `"50.00"`, 123 → `"1.23"`); no binary floating
   point arithmetic anywhere on the path.
5. **No Formance → no crash:** if `FORNANCE_LEDGER_URL` is unset or Formance is
   unreachable, posting returns `None` gracefully and Postgres behaviour is
   unchanged (existing tests keep passing).
6. **Idempotency metadata:** each Formance transaction carries `reference`
   (JE ref) and `metadata.transaction_id` (Postgres UUID), unchanged from v2.
7. **Zero regression:** the full existing suite plus new unit tests passes.

## Interfaces

- `TransactionService._post_to_formance(db, transaction) -> Optional[dict]`
  (signature unchanged)
- `FormanceClient.create_transaction(ledger, postings, reference, timestamp,
  metadata)` (signature unchanged; `postings[i]["amount"]` becomes a decimal
  string)
- New pure helper (testable directly):
  `build_formance_postings(postings: list[PostingCreate]) -> list[dict]`

## Test Targets

- `api/tests/unit/test_formance_mirroring.py` — new: pairing, compound,
  reversal, decimal conversion (including £0.01 and large amounts),
  unbalanced/zero entries rejected upstream, graceful failure when Formance
  unavailable.
