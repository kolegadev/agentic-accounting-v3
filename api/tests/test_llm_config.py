"""Contract F33 — runtime LLM configuration: store + API routes.

The settings modal saves {api_url, model, api_key}; the store takes effect
immediately (orchestrator/llm_router read it per call) and persists to
LLM_CONFIG_PATH when set.  The API key is never returned unmasked.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.services import llm_config


@pytest.fixture(autouse=True)
def _restore_config():
    before = llm_config.get_config()
    before_path = llm_config.CONFIG_PATH
    yield
    llm_config.update_config(before)
    llm_config.CONFIG_PATH = before_path


def _masked_keys(payload: dict) -> set[str]:
    return set(payload.keys())


@pytest.mark.asyncio
async def test_update_changes_live_config() -> None:
    llm_config.update_config(
        {"model": "deepseek-flash", "api_key": "sk-live-abcdef"}
    )
    assert llm_config.get_model() == "deepseek-flash"
    assert llm_config.get_api_key() == "sk-live-abcdef"


@pytest.mark.asyncio
async def test_update_ignores_empty_fields() -> None:
    before = llm_config.get_config()
    llm_config.update_config({"model": "", "api_url": "   "})
    assert llm_config.get_config() == before


@pytest.mark.asyncio
async def test_persists_to_config_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "llm_config.json"
    monkeypatch.setattr(llm_config, "CONFIG_PATH", str(path))
    llm_config.update_config({"model": "deepseek-flash"})
    saved = json.loads(path.read_text())
    assert saved["model"] == "deepseek-flash"


def test_get_route_masks_key() -> None:
    llm_config.update_config({"api_key": "sk-secret-1234567890"})
    client = TestClient(app)
    resp = client.get("/api/v1/chat/llm-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_key"] is True
    assert "1234567890" not in json.dumps(body)
    assert body["api_key"].endswith("***")


def test_post_route_updates_and_reports() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/chat/llm-config", json={"model": "deepseek-flash"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["config"]["model"] == "deepseek-flash"
    assert llm_config.get_model() == "deepseek-flash"


def test_post_route_ignores_empty_body() -> None:
    client = TestClient(app)
    before = llm_config.get_config()
    resp = client.post("/api/v1/chat/llm-config", json={})
    assert resp.status_code == 200
    assert llm_config.get_config() == before
