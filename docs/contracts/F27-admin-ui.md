# CONTRACT F27 — Restore Admin Capabilities on the Real Pipeline

## Goal

F23 removed the v2 mock world AND the only UI for company admin — a real
functional regression. This contract restores every capability, backed by
the REAL API: add a company (entity + COA + bank + opening balances),
view/edit company details, contacts management, a users list, and the
settings modal — all scoped and wired to the real endpoints.

## Changes (exact)

### A. API (`api/`)

1. `api/src/routes/entities.py` — NEW `PATCH /api/v1/entities/{entity_id}`
   (auth `require_role("owner","admin")`) → `EntityService.update_entity`;
   404 EntityNotFoundError mapping; 200 with the updated EntityResponse.
   Route tests for 200/404/403.

### B. chat-ui server (`chat-ui/server.py`)

2. Owner-token injection: env `OWNER_EMAIL`/`OWNER_PASSWORD` (defaults
   `john@kolega.ai` / `TestAccounting123!`); the server logs in ONCE
   (lazy, cached, `POST /api/v1/auth/login`) and exposes
   `_admin_auth_headers()`; a failed login degrades to empty headers with
   a logged warning (the UI shows the upstream 401).
3. NEW proxies over the `_upstream_client` seam:
   - `POST /api/entities` → `POST /api/v1/entities/` (body + admin
     Authorization header; query string forwarded)
   - `PATCH /api/entities/{entity_id}` → `PATCH /api/v1/entities/{id}`
     (same)
   - `GET /api/contacts` → `GET /api/v1/contacts/` (no token needed —
     contacts are unauthenticated)
   - `POST /api/contacts` → `POST /api/v1/contacts/` (body)
   - `GET /api/users` → `GET /api/v1/auth/users` (admin header)
   All: pass-through status/JSON, 502 `upstream_unreachable` on transport
   failure, NO mock fallback.

### C. chat-ui page (`chat-ui/index.html`)

4. Topbar "+ Add company" button (`#add-company-btn`) → modal
   (`#add-company-overlay`) form: name, legal_type (select: sole_trader,
   limited_company, partnership, micro_entity, property_landlord, other),
   framework (select uk-gaap/ifrs), fiscal_year_start (MM-DD), VAT
   checkbox + vat_number + vat_scheme (select), optional bank name +
   opening balance (£) → `POST /api/entities` with a `setup` payload
   (template_name derived from legal_type + VAT: uk_<type>_vat /
   uk_<type>_no_vat per the existing fuzzy loader) → on success reload
   `loadEntitiesFromAPI()` + switch to the new entity + close modal; on
   error show the message in the modal.
5. "Company details" admin modal (`#company-admin-btn`,
   `#company-admin-overlay`): shows the selected entity's fields; Edit →
   editable form; Save → `PATCH /api/entities/{id}`; Cancel reverts. Also
   lists USERS (`#users-list`, read-only name/email/role from
   `/api/users`).
6. Contacts panel (`#contacts-panel`) in the left column: list contacts
   (`#contacts-list`: name, type, email from `/api/contacts`) + add form
   (`#contact-name`, `#contact-type` select customer/supplier/both/other,
   `#contact-email`, `#contact-add`) → `POST /api/contacts` → re-list.
   Contacts are global (not entity-scoped) — label the panel accordingly.
7. Settings modal stays exactly as-is (API URL + LLM config).
8. Entity switch keeps its F23 fan-out (pending + Formance + books +
   preview clear) and additionally re-renders the company-details modal if
   open.

### D. Tests

9. `chat-ui/tests/test_server.py` — new tests: POST/PATCH /api/entities
   forward body + Authorization header (mock transport captures headers);
   GET/POST /api/contacts forwarding (no token asserted); GET /api/users
   forwards the admin header; 502 shapes; login-called-once caching
   (token fetched once across calls).
10. `api/tests/test_chatui_contract.py` — new DOM ids asserted:
    `#add-company-btn`, `#add-company-overlay`, `#company-admin-btn`,
    `#company-admin-overlay`, `#contacts-panel`, `#contacts-list`,
    `#contact-name`, `#contact-type`, `#contact-email`, `#contact-add`,
    `#users-list`; endpoint strings `/api/contacts`, `/api/users`.
11. `api/tests/test_entity_routes.py` — the PATCH route tests (200/404/403).
12. Suites: api ≥ 1319, chat-ui ≥ 38, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/routes/entities.py` (PATCH route only),
  `chat-ui/server.py`, `chat-ui/index.html`,
  `chat-ui/tests/test_server.py`, `api/tests/test_chatui_contract.py`,
  `api/tests/test_entity_routes.py`, this contract.
- NOT: constructors, catalog, report engine, parity, transaction_service,
  orchestrator, Formance MCP, models, migrations.

## Success criteria

1. Suites green (above).
2. `git status --porcelain -uall` = allowed paths only.
3. After deploy: a browser check can add a second company (creates a real
   entity with its own COA/ledger), edit its details, add a contact, see
   the users list, and switch between the two entities with every panel
   re-scoping.
