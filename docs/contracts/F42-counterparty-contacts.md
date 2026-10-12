# CONTRACT F42 — Counterparty in Transaction Records + Rich Editable Contacts

## Goal

Two fixes from John's manual test:

1. The account drill-down (F37) and the AR/AP aging items (F37) show
   only the JE reference + amounts — no Supplier/Buyer.  When working an
   Aged Payables list you must be able to see WHO the money is owed to.
   Every transaction carries `contact_id` (stamped by F29's contact
   auto-creation), so surface the contact name wherever transactions are
   listed.
2. Contacts can be created but not edited, and the add form only has
   name/type/email — the Contact MODEL and the PATCH route already carry
   company / phone / address_line1 / address_line2 / city / postcode /
   country; the UI never exposes them.

## Changes (exact)

### A. Counterparty names

1. `api/src/services/transaction_service.py`
   (`list_for_account` ONLY): join `Contact` on `Transaction.contact_id`;
   each item gains `contact_name` (str or None).  Unknown/missing contact
   → None (never fail the query).
2. `api/src/routes/transactions.py` (the drill-down route): pass
   `contact_name` through in each item.
3. `api/src/services/formance_reports.py` (`_aging_legs` / `_generate_aging`):
   each aging item gains `contact_name` (join Contact on contact_id;
   fallback None).  The AR/AP route shape then carries it per item.
4. `api/src/routes/formance_reports.py`: the aging response items include
   `contact_name` (no other shape change).
5. `chat-ui/index.html`:
   - the account-modal items table (F37) gains a **Counterparty** column
     (`—` when absent);
   - the aging renderer's items table gains a **Counterparty** column.

### B. Rich + editable contacts (UI)

6. `chat-ui/server.py`: add the `PATCH /api/contacts/{contact_id}` proxy
   (owner-token pattern like POST /api/contacts; 502 pattern; standard
   client).
7. `chat-ui/index.html` contacts panel (F27):
   - the ADD form gains: company, named contact person (uses the model's
     `name`? NO — the model has no person field; the display `name` stays
     as-is and `company` is the trading name — so the add form gains
     company, phone, address_line1, address_line2, city, postcode,
     country; keep name as the display name),
   - an **Edit** button on each contact row → a contact-edit modal
     pre-filled with ALL fields (name, type, company, email, phone,
     address lines, city, postcode, country) → PATCH via the new proxy →
     reload the list.  esc() everywhere; the list rendering gains the
     company + phone in the row subtitle.
8. Optional-but-expected: the contacts list shows company + email + phone
   per row (currently name/type only) so the richer data is visible.

### C. Tests

9. Service/route: list_for_account items carry contact_name (present +
   NULL cases); aging items carry contact_name; route shapes pass them
   through.
10. chat-ui: contract strings (edit-modal ids, PATCH proxy path,
    Counterparty column) + proxy test for PATCH passthrough.
11. Full suite: api ≥ 1437, chat-ui ≥ 55, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/services/transaction_service.py`
  (list_for_account), `api/src/services/formance_reports.py`
  (aging items), `api/src/routes/transactions.py`,
  `api/src/routes/formance_reports.py`, `chat-ui/server.py`,
  `chat-ui/index.html`, the test files, this contract.
- NOT: Contact model/validators/routes (already complete), constructors,
  catalog, orchestrator, report engine identities.

## Success criteria

1. Suites green (above).
2. Live: the 1100 drill-down shows Zephyr/Jost names on their rows; the
   aged-payables items name the suppliers; editing a contact's phone +
   address persists and shows in the list.
