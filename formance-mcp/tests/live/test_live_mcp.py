"""Contract F15 — live MCP round-trip against the REAL Formance ledger.

Runs the fixed ``formance_mcp`` client/tools against a genuine Formance
ledger (v2.3.21) for the first time: Numscript submission with minor-unit
integer amounts, credit-nature sources via unbounded overdraft, and the
cursor-embedded envelope normalisation.

Every test is decorated with a module-level ``pytestmark`` skip that checks
the environment ONCE per pytest session (env var set AND ``/_info`` returns
200); on hosts without a real ledger (e.g. this Mac) the whole module skips
cleanly and the local suite stays green.

Run (on thebrick, from /tmp/aa-live/formance-mcp):
  FORNANCE_LEDGER_URL=http://127.0.0.1:3068 .venv/bin/python -m pytest \\
      tests/live -v --timeout=120
See docs/live-e2e.md for the full runbook.
"""

from __future__ import annotations

import functools
import os
import random
import string

import httpx
import pytest

import formance_mcp.server as server_mod
from formance_mcp.client import FormanceClient

# ---------------------------------------------------------------------------
# Environment gate — evaluated once per pytest session
# ---------------------------------------------------------------------------

FORNANCE_LEDGER_URL = os.getenv("FORNANCE_LEDGER_URL", "").rstrip("/")


@functools.lru_cache(maxsize=1)
def _live_reachable() -> bool:
    """True when the env var is set AND the real ledger answers ``/_info``.

    Cached: the reachability probe runs exactly once per pytest session
    (contract F14's skipif helper, mirrored).
    """
    if not FORNANCE_LEDGER_URL:
        return False
    try:
        resp = httpx.get(f"{FORNANCE_LEDGER_URL}/_info", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _live_reachable(),
    reason=(
        "LIVE suite: FORNANCE_LEDGER_URL not set or the real Formance ledger "
        "is unreachable (_info != 200) — skipped on hosts without a ledger"
    ),
)


def _client() -> FormanceClient:
    return FormanceClient(base_url=FORNANCE_LEDGER_URL)


async def test_live_mcp_round_trip(monkeypatch) -> None:
    """create ledger -> credit-nature-source £100.00 -> balances signs ->
    list normalisation — through the REAL MCP client/tools.

    Source @4000 (a credit-nature account) has no funds; the unbounded
    overdraft clause in the generated Numscript is what lets it go to −£100
    while @1000 gains +£100.  The MCP public shape stays decimal strings in
    ("100.00") and decimal strings out ("100.00" / "-100.00").
    """
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    ledger = f"mcp-live-{suffix}"
    client = _client()
    monkeypatch.setattr(server_mod, "get_client", lambda: client)

    # (a) create_ledger: 204 on first call, 400 LEDGER_ALREADY_EXISTS on
    #     repeat — both idempotent successes through the client.
    assert await client.create_ledger(ledger) is True
    assert await client.create_ledger(ledger) is True

    # (b) create_transaction through the REAL tool: credit-nature source
    #     @4000 -> destination @1000, £100.00.
    result = await server_mod.create_transaction(
        ledger,
        [
            {
                "source": "4000",
                "destination": "1000",
                "amount": "100.00",
                "asset": "GBP/2",
            }
        ],
        reference="F15-LIVE-1",
        metadata={"transaction_id": "f15-live"},
        idempotency_key=f"f15-{suffix}",
    )
    assert isinstance(result, dict)
    assert isinstance(result.get("txid"), int), f"txid shape: {result!r}"

    # (c) get_balances through the REAL tool: +£100.00 on 1000, −£100.00 on
    #     4000 (minor-unit +10000 / −10000 on the wire).
    balances = await server_mod.get_balances(ledger)
    by_address = {row["address"]: row["balances"] for row in balances["data"]}
    assert by_address["1000"]["GBP/2"] == "100.00", by_address
    assert by_address["4000"]["GBP/2"] == "-100.00", by_address

    # (d) list_transactions through the REAL tool: the tx comes back with
    #     decimal-string amounts and the flat cursor envelope.
    listed = await server_mod.list_transactions(ledger)
    assert listed["cursor"] == {"hasMore": False, "next": None}
    txs = listed["data"]
    assert len(txs) == 1
    tx = txs[0]
    assert tx["txid"] == result["txid"]
    assert tx["reference"] == "F15-LIVE-1"
    assert tx["metadata"]["transaction_id"] == "f15-live"
    assert tx["postings"] == [
        {
            "source": "4000",
            "destination": "1000",
            "amount": "100.00",
            "asset": "GBP/2",
        }
    ]

    # (e) raw wire check: the real API reports minor-unit integers
    #     (+10000 / −10000) inside the cursor envelope.
    async with httpx.AsyncClient(timeout=10.0) as hc:
        raw = await hc.get(f"{FORNANCE_LEDGER_URL}/{ledger}/balances")
        raw.raise_for_status()
        payload = raw.json()
        raw_cursor = payload["cursor"]
        raw_rows = {addr: assets for row in raw_cursor["data"]
                    for addr, assets in row.items()}
        assert raw_rows["1000"]["GBP/2"] == 10000, raw_rows
        assert raw_rows["4000"]["GBP/2"] == -10000, raw_rows
