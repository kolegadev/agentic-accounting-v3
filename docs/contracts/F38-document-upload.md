# CONTRACT F38 — Document Upload: Extract Transactions from PDF/JPG

## Goal

John: "upload a pdf or jpg of an invoice or a bill or a payroll record
into the chat window and be able to read the pdf and extract the
transactions ready for review and approval."

## Design (v1)

The uploaded document's TEXT (never an image) goes to the SAME
classification + deterministic-constructor pipeline as chat messages, so
the preview/approve flow is unchanged and the LLM never does the money
math.  One document → one transaction draft (multi-line documents get a
clarification for the first line; multi-transaction batch import is
explicitly out of scope).

## Changes (exact)

### A. Extraction helper (`api/src/services/document_extract.py` — NEW)

1. `extract_document_text(filename: str, data: bytes) -> str`:
   - `.pdf` → `pdftotext -layout <file> -` via subprocess (binary at
     `/usr/bin/pdftotext`; timeout 20s).  If the text is blank/short
     (<40 chars), try pypdf: extract embedded images → OCR each with
     pytesseract → concatenate.  No pypdf → skip the image pass.
   - `.jpg/.jpeg/.png` → PIL open → pytesseract.image_to_string.
   - Import-guard the optional deps (pypdf, pytesseract) at call time;
     missing → structured `ocr_unavailable` error (never a crash).
   - Raise `DocumentExtractError(message, type)` with types
     `document_unreadable` / `ocr_unavailable` — the route maps them to
     422.
2. No file persistence: extraction is in-memory/tempfile only.

### B. Orchestrator (`api/src/services/orchestrator.py`)

3. Refactor WITHOUT changing existing behaviour: pull the classify →
   construct → contact → preview path of `orchestrate_message` into a
   shared internal helper (e.g. `_classify_and_preview(db, message, *,
   system_prompt, scope_id, ...)`) so `orchestrate_message` and the new
   `orchestrate_document` both use it.  ALL 1396 api tests must stay
   green unmodified (except new ones).
4. New `orchestrate_document(db, text, *, filename, entity_id=None,
   history=None) -> OrchestrateResult`: resolve the scope + COA exactly
   like `orchestrate_message`; build the system prompt via
   `build_system_prompt(history, coa=coa_rows)` PLUS a
   `## Document text (extract ONE transaction from it)` section
   (filename + the extracted text, truncated to ~6000 chars); user
   message: "Extract the transaction from the attached document. If
   anything is ambiguous, ask a clarification. Respond ONLY with JSON.";
   run the shared classify/preview path (same prose-retry, same contact
   auto-creation); on success `result.source_text` carries the first
   ~500 chars of the extracted text for the UI to show; structured
   errors pass through (clarification / invalid_response / etc.).

### C. Route (`api/src/routes/orchestrator.py`)

5. New `POST /api/v1/orchestrator/document` (multipart):
   - Form: `file: UploadFile` (required), `entity_id` (optional),
     `history` (optional JSON string).
   - Guards: content-type/ext whitelist (pdf/jpg/jpeg/png) → 415
     `unsupported_document_type`; size ≤ 10MB → 413 `document_too_large`.
   - `extract_document_text` → `orchestrate_document` → response:
     - preview → 200 {status, preview{...same shape as /message},
       source_text, filename}
     - clarification → 200 {status: clarification, error}
     - structured errors → 422 {type, message} (same style as /message).
   - The route already has the AmbiguousEntity handling — mirror the
     `/message` route's scope resolution.

### D. chat-ui

6. `chat-ui/server.py`: proxy `POST /api/orchestrator/document` —
   multipart passthrough (read `request` body via `await request.body()`
   and forward with the content-type header, OR use httpx `files=` from
   `UploadFile` — pick the approach that passes bytes through untouched;
   keep the F20 query-param pattern for entity_id if present), using the
   65s LLM client (document classify can be slow), same 502 pattern.
7. `chat-ui/index.html`: a paperclip button next to the chat input +
   hidden `<input type="file" accept=".pdf,.jpg,.jpeg,.png">`; on file
   select: show "Reading document…" status, POST multipart to the proxy
   (append entity_id), then drive the SAME renderPreview/approve flow;
   on preview show a collapsible "Extracted text" panel (source_text,
   escaped); on clarification/error show the message in the chat; after
   success clear the file input.  The paperclip also updates chatHistory
   (push the user turn "Uploaded <filename>" so the conversation stays
   coherent).

### E. Tests

8. Unit (`api/tests/unit/test_document_extract.py` — NEW): fake
   pdftotext (monkeypatched subprocess) text pass-through; blank-pdf →
   embedded-image OCR path (fake pytesseract); jpg → fake OCR; missing
   binaries → structured errors; type/size guards via route tests.
9. Orchestrator unit: `orchestrate_document` with a fake LLM returning
   supplier-invoice facts → preview + legs + contact auto-created;
   clarification passes through; the prompt contains the document text
   and filename.
10. Route tests (test_orchestrator_routes.py): multipart upload shape,
    415/413 guards, 422 unreadable, 200 preview passthrough with
    source_text.
11. chat-ui contract test: paperclip/file-input ids + proxy path string
    presence; proxy test in test_server.py (multipart bytes forwarded).
12. Full suite: api ≥ 1396, chat-ui ≥ 50, formance-mcp 60 — all green.
    Tests that need tesseract/pypdf binaries must skip cleanly when
    absent (they are present in the deploy env after the deploy step).

## Deploy steps (director, after keep)

- thebrick: `sudo apt-get install -y tesseract-ocr` (passwordless sudo
  verified working) + `pip install pytesseract pypdf` into
  `/tmp/aa-run/api/.venv`.
- Live check: generate a small text PDF invoice, upload it through the
  proxy, expect a supplier-invoice preview with the PDF's amounts.

## Boundaries

- ALLOWED: `api/src/services/document_extract.py` (NEW),
  `api/src/services/orchestrator.py`, `api/src/routes/orchestrator.py`,
  `chat-ui/server.py`, `chat-ui/index.html`, the test files, this
  contract, `api/pyproject.toml`/requirements (add pypdf + pytesseract
  as dependencies).
- NOT: constructors, catalog, engine, transaction/contact services,
  other routes, formance-mcp.

## Success criteria

1. Suites green (above).
2. Live: a PDF invoice upload produces a correct preview with legs
   (amounts from the document), approvable as usual; a JPG upload OCRs
   (tesseract installed) — both verified through the UI proxy.
