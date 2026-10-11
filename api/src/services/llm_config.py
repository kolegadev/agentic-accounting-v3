"""Runtime LLM configuration with optional file persistence (Contract F33).

The settings modal saves {api_url, model, api_key}; the orchestrator and
llm_router read the LIVE config through these getters instead of import-time
env constants, so a save takes effect without a restart.  ``LLM_CONFIG_PATH``
(optional env) persists the last saved config across restarts; env vars
remain the initial defaults.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.getenv("LLM_CONFIG_PATH", "")

_lock = threading.Lock()
_config: dict[str, str] = {
    "api_url": os.getenv(
        "LLM_API_URL", "https://api.deepseek.com/v1/chat/completions"
    ),
    "model": os.getenv("LLM_MODEL", "deepseek-v4-flash"),
    "api_key": os.getenv("LLM_API_KEY", ""),
}

_FIELDS = ("api_url", "model", "api_key")


def _load_from_disk() -> None:
    if not CONFIG_PATH:
        return
    path = Path(CONFIG_PATH)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("LLM config file unreadable (%s): %s", CONFIG_PATH, exc)
        return
    for field in _FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            _config[field] = value


def get_config() -> dict[str, str]:
    """Full config — callers must mask the key before exposing it."""
    return dict(_config)


def get_api_url() -> str:
    return _config["api_url"]


def get_model() -> str:
    return _config["model"]


def get_api_key() -> str:
    return _config["api_key"]


def update_config(fields: dict[str, str]) -> dict[str, str]:
    """Apply non-empty fields; persist when LLM_CONFIG_PATH is set."""
    with _lock:
        for field in _FIELDS:
            value = fields.get(field)
            if isinstance(value, str) and value.strip():
                _config[field] = value.strip()
        if CONFIG_PATH:
            try:
                Path(CONFIG_PATH).write_text(json.dumps(_config))
            except OSError as exc:
                logger.warning("LLM config file unwritable (%s): %s", CONFIG_PATH, exc)
    return dict(_config)


_load_from_disk()
