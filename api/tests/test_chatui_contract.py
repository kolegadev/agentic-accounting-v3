# /Users/johnpellew/agentic-accounting-v3/api/tests/test_chatui_contract.py

"""Contract F7 — static DOM contract test for the chat-ui frontend.

Reads chat-ui/index.html directly (no browser harness) and asserts the
7 required preview/approval DOM ids exist, and that the JavaScript
references all 4 orchestrator endpoints.  Prevents UI drift: if the pane
is renamed or the wiring is removed, this fails.
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
