# /Users/johnpellew/agentic-accounting-v3/api/tests/test_chatui_contract.py

"""Contract F7 + F10 — static DOM contract test for the chat-ui frontend.

Reads chat-ui/index.html directly (no browser harness) and asserts the
7 required preview/approval DOM ids exist, and that the JavaScript
references all 4 orchestrator endpoints.  Also asserts the F10 reports
pane: the 8 report DOM ids and the /api/reports/formance/ endpoint
reference.  Prevents UI drift: if a pane is renamed or the wiring is
removed, this fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CHATUI_INDEX = Path(__file__).resolve().parents[2] / "chat-ui" / "index.html"

REQUIRED_DOM_IDS = [
    "orchestrator-preview",
    "preview-legs",
    "preview-total",
    "preview-approve",
    "preview-reject",
    "preview-status",
    "pending-drafts",
]

ORCHESTRATOR_ENDPOINTS = [
    "/api/orchestrator/message",
    "/api/orchestrator/approve",
    "/api/orchestrator/reject",
    "/api/orchestrator/pending",
]

REPORT_DOM_IDS = [
    "reports-pane",
    "report-type-select",
    "report-period-start",
    "report-period-end",
    "report-as-at",
    "report-fetch",
    "report-output",
    "report-crosscheck",
]

REPORT_ENDPOINTS = [
    "/api/reports/formance/",
]

# Contract F20 — the entity switcher: stable DOM ids, the entity list
# endpoint, and the entity_id query wiring in the JS.
ENTITY_DOM_IDS = [
    "entity-select",
    "entity-label",
]

ENTITY_ENDPOINTS = [
    "/api/entities",
]

# Contract F23 — the Books-in-plain-English panel + the mock-world strings
# that must be GONE from the rebuilt page.
BOOKS_DOM_IDS = [
    "books-panel",
    "books-output",
]

BOOKS_ENDPOINTS = [
    "/api/books",
]

# Contract F27 — the restored admin surface: add company, company details,
# contacts (global) and the users list, all wired to the real pipeline.
ADMIN_DOM_IDS = [
    "add-company-btn",
    "add-company-overlay",
    "company-admin-btn",
    "company-admin-overlay",
    "contacts-panel",
    "contacts-list",
    "contact-name",
    "contact-type",
    "contact-email",
    "contact-add",
    "users-list",
]

ADMIN_ENDPOINTS = [
    "/api/contacts",
    "/api/users",
]

FORBIDDEN_STRINGS = [
    "brightwork",
    "/api/companies",
    "/api/dashboard",
    "/ws/chat",
]


@pytest.fixture(scope="module")
def html() -> str:
    assert CHATUI_INDEX.exists(), f"chat-ui/index.html missing at {CHATUI_INDEX}"
    return CHATUI_INDEX.read_text(encoding="utf-8")


@pytest.mark.parametrize("dom_id", REQUIRED_DOM_IDS)
def test_required_dom_id_present(html: str, dom_id: str) -> None:
    assert f'id="{dom_id}"' in html


@pytest.mark.parametrize("endpoint", ORCHESTRATOR_ENDPOINTS)
def test_js_references_orchestrator_endpoint(html: str, endpoint: str) -> None:
    assert endpoint in html


@pytest.mark.parametrize("dom_id", REPORT_DOM_IDS)
def test_required_report_dom_id_present(html: str, dom_id: str) -> None:
    assert f'id="{dom_id}"' in html


@pytest.mark.parametrize("endpoint", REPORT_ENDPOINTS)
def test_js_references_report_endpoint(html: str, endpoint: str) -> None:
    assert endpoint in html


@pytest.mark.parametrize("dom_id", ENTITY_DOM_IDS)
def test_required_entity_dom_id_present(html: str, dom_id: str) -> None:
    assert f'id="{dom_id}"' in html


@pytest.mark.parametrize("endpoint", ENTITY_ENDPOINTS)
def test_js_references_entities_endpoint(html: str, endpoint: str) -> None:
    assert endpoint in html


@pytest.mark.parametrize("dom_id", BOOKS_DOM_IDS)
def test_required_books_dom_id_present(html: str, dom_id: str) -> None:
    assert f'id="{dom_id}"' in html


@pytest.mark.parametrize("endpoint", BOOKS_ENDPOINTS)
def test_js_references_books_endpoint(html: str, endpoint: str) -> None:
    assert endpoint in html


@pytest.mark.parametrize("dom_id", ADMIN_DOM_IDS)
def test_required_admin_dom_id_present(html: str, dom_id: str) -> None:
    assert f'id="{dom_id}"' in html


@pytest.mark.parametrize("endpoint", ADMIN_ENDPOINTS)
def test_js_references_admin_endpoint(html: str, endpoint: str) -> None:
    assert endpoint in html


@pytest.mark.parametrize("forbidden", FORBIDDEN_STRINGS)
def test_mock_world_string_absent(html: str, forbidden: str) -> None:
    """Contract F23: the mock world is gone — no mock company identifiers,
    mock dashboard/companies endpoints, or the WS chat bridge may remain in
    the rebuilt page."""
    assert forbidden not in html


def test_entity_id_query_wiring_present(html: str) -> None:
    """The JS wires ?entity_id=<selected> into EVERY orchestrator/report
    call when an entity is selected — pinned per call site so a future
    regression that drops one call site's scope is caught."""
    assert "entity_id=" in html
    # approve / reject / pending fetch through the scoped helper
    assert "entityUrl('/api/orchestrator/approve')" in html
    assert "entityUrl('/api/orchestrator/reject')" in html
    assert "entityUrl('/api/orchestrator/pending')" in html
    # message carries the entity in the request body
    assert "body.entity_id = AppState.entityId" in html
    # report fetch appends the entity param
    assert "var eqs = entityParam();" in html
    assert "'/api/reports/formance/'" in html


def test_message_carries_conversation_history(html: str) -> None:
    """Contract F35 — every orchestrator message carries the bounded
    conversation history (user turns + clarification/preview assistant
    turns), so the clarification loop remembers what was already agreed."""
    assert "body.history = chatHistory" in html
    assert "pushChatHistory('user', text)" in html
    assert "pushChatHistory('assistant', clar)" in html
    assert "chatHistory.length > 14" in html


def test_per_pane_generation_counters(html: str) -> None:
    """Contract F23 — loadFormanceReport() and loadBooks() each own a
    generation counter, and each loader guards BOTH of its DOM writes
    (success path and catch path) against its own counter.

    A single shared reqGen breaks the Formance pane: init() and
    switchEntity() call loadFormanceReport() then loadBooks() synchronously,
    so the books call's increment makes the formance response look stale
    forever and the reports pane sticks on 'Loading…'.  Reverting to a
    shared counter, or removing either guard, must fail this test."""
    assert "var myGen = ++formanceGen" in html
    assert "var myGen = ++booksGen" in html
    assert html.count("myGen !== formanceGen") >= 2
    assert html.count("myGen !== booksGen") >= 2


def test_esc_escapes_double_quotes_for_attribute_contexts(html: str) -> None:
    """Contract F23 — esc() escapes double quotes, so table-cell values
    interpolated into data-value="…" attributes in renderTable cannot
    inject attributes (e.g. a cell containing '" onmouseover=…')."""
    assert ".replace(/\"/g, '&quot;')" in html
    # pin that renderTable's data-value uses the ESCAPED cell (not the raw value)
    assert "data-value=\"' + esc(cells[j].trim()) + '\"" in html


def test_setup_template_formula_pinned(html: str) -> None:
    """Contract F27 — the COA template name is derived client-side as
    'uk_' + legalType + (vatRegistered ? '_vat' : '_no_vat').  Pinning the
    literal expression keeps the fuzzy template loader in step with the
    admin UI: inverting _vat/_no_vat (or swapping the branches) must fail
    this test."""
    assert (
        "template_name: 'uk_' + legalType + (vatRegistered ? '_vat' : '_no_vat')"
        in html
    )


def test_opening_balance_pounds_to_pence(html: str) -> None:
    """Contract F27 — the add-company opening balance is converted from
    pounds to pence with Math.round(parseFloat(...) * 100) before it
    becomes the bank opening_balance (pence) in the setup payload."""
    assert "Math.round(parseFloat(openingStr || '0') * 100)" in html
    assert "opening_balance: openingPence" in html


def test_fiscal_year_start_never_null(html: str) -> None:
    """Contract F27 — fiscal_year_start defaults to '04-06' in BOTH admin
    saves; null must never reach the API (the backend fiscal-year parser
    crashes on NULL)."""
    assert "fiscal_year_start: fys || '04-06'" in html
    assert (
        "fiscal_year_start: document.getElementById('co-fys').value.trim() || '04-06'"
        in html
    )
    assert (
        "fiscal_year_start: document.getElementById('co-fys').value.trim() || null"
        not in html
    )
