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


def test_entity_id_query_wiring_present(html: str) -> None:
    """The JS wires ?entity_id=<selected> into the orchestrator/report
    calls when an entity is selected."""
    assert "entity_id=" in html
