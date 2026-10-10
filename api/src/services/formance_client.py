"""Formance Ledger v2 HTTP client adapter.

This adapter is the ONLY module that knows the real ledger's wire format
(observed against ``ghcr.io/formancehq/ledger`` v2.3.21 — see
docs/live-e2e.md).  The rest of the system (mirror, parity verifier, F9
report engine) keeps speaking the canonical shapes below, unchanged:

- ``create_transaction``: amounts are minor-unit JSON integers on the wire
  and the engine rejects any posting that would make a non-``world`` account
  negative (``INSUFFICIENT_FUND``).  Mirrors are therefore submitted as
  Numscript with ``allowing unbounded overdraft`` on every source, so
  credit-nature accounts hold negative balances exactly as the parity
  verifier expects.  F1 pairing (which accounts pair, the ``<currency>/2``
  asset convention, the major-unit amounts) is NOT changed — this adapter
  only re-encodes the same postings.
- ``create_ledger``: ``POST /v2/{ledger}`` answers 204 for a new ledger and
  400 ``LEDGER_ALREADY_EXISTS`` for a repeat (the old 409 is also tolerated).
- ``list_transactions`` / ``get_balances``: the real envelope embeds the
  data list INSIDE ``cursor`` and reports amounts as minor-unit integers;
  this adapter normalises to ``{"cursor": {...}, "data": [...]}`` with
  major-unit decimal strings — the shape every fake and caller uses.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

FORNANCE_URL = os.getenv("FORNANCE_LEDGER_URL", "http://localhost:3068")

# Real create-ledger answer codes: 200/201/204 = created (or already
# created), 400 LEDGER_ALREADY_EXISTS / 409 = duplicate — all idempotent
# successes for our purposes.
_CREATE_LEDGER_OK = (200, 201, 204)
_CREATE_LEDGER_DUPLICATE = (400, 409)


def _asset_precision(asset: Any) -> int:
    """Minor-unit exponent of a Formance asset (``GBP/2`` -> 2)."""
    text = str(asset or "")
    if "/" in text:
        try:
            return int(text.rsplit("/", 1)[1])
        except ValueError:
            return 2
    return 2


def _major_to_minor(amount: Any, asset: Any) -> int:
    """Exact major-unit decimal string -> minor-unit integer.

    ``"10000.00"`` with asset ``GBP/2`` -> ``1000000``.  A major-unit amount
    that is not exactly representable in the asset's minor units is refused
    (never silently truncated).
    """
    precision = _asset_precision(asset)
    value = Decimal(str(amount)) * (10**precision)
    if value != value.to_integral_value():
        raise ValueError(
            f"amount {amount!r} is not representable in {precision} minor units"
        )
    return int(value)


def _minor_to_major(amount: Any, asset: Any) -> str:
    """Minor-unit integer -> exact major-unit decimal string."""
    precision = _asset_precision(asset)
    quantum = Decimal(1).scaleb(-precision)
    return str((Decimal(str(amount)) / (10**precision)).quantize(quantum))


def _build_numscript(postings: list[dict[str, Any]]) -> str:
    """One ``send`` statement per posting, every source allowed to overdraw.

    The overdraft clause is what lets credit-nature accounts (equity,
    liabilities, revenue, VAT) hold the negative balances double-entry
    bookkeeping requires; the real engine otherwise rejects any posting that
    would take a non-``world`` account below zero.
    """
    statements: list[str] = []
    for posting in postings:
        asset = str(posting.get("asset") or "")
        amount = _major_to_minor(posting.get("amount"), asset)
        statements.append(
            f"send [{asset} {amount}] (\n"
            f"\tsource = @{posting.get('source')} allowing unbounded overdraft\n"
            f"\tdestination = @{posting.get('destination')}\n"
            ")"
        )
    return "\n".join(statements)


def _normalise_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    """Real wire transaction -> canonical shape (major-unit amount strings)."""
    postings = []
    for posting in tx.get("postings") or []:
        if not isinstance(posting, dict):
            continue
        asset = str(posting.get("asset") or "")
        postings.append(
            {
                "source": str(posting.get("source") or ""),
                "destination": str(posting.get("destination") or ""),
                "amount": _minor_to_major(posting.get("amount"), asset),
                "asset": asset,
            }
        )
    return {
        "txid": tx.get("txid"),
        "reference": tx.get("reference"),
        "timestamp": tx.get("timestamp"),
        "metadata": tx.get("metadata") or {},
        "postings": postings,
    }


def _cursor_data(payload: Any) -> tuple[dict[str, Any], list[Any]]:
    """Split a real envelope (data inside ``cursor``) into cursor + rows."""
    if not isinstance(payload, dict):
        return {}, []
    cursor = payload.get("cursor")
    if isinstance(cursor, dict):
        return cursor, cursor.get("data") or []
    return {}, payload.get("data") or []


class FormanceClient:
    """Async HTTP client for Formance Ledger v2."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or FORNANCE_URL).rstrip("/")

    async def create_transaction(
        self,
        ledger: str,
        postings: list[dict[str, Any]],
        reference: Optional[str] = None,
        timestamp: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create and commit a transaction to the ledger.

        Args:
            ledger: Ledger name (e.g., "main").
            postings: List of posting dicts with source, destination,
                      amount (major-unit decimal string), and asset fields.
            reference: External reference (e.g., JE-2026-0001).
            timestamp: ISO 8601 timestamp for the transaction.
            metadata: Arbitrary key-value metadata (string values).
        """
        body: dict[str, Any] = {"script": {"plain": _build_numscript(postings)}}
        if reference:
            body["reference"] = reference
        if timestamp:
            body["timestamp"] = timestamp
        if metadata:
            body["metadata"] = metadata

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/{ledger}/transactions",
                json=body,
            )
            resp.raise_for_status()
            payload = resp.json()

        rows: list[Any] = []
        if isinstance(payload, dict):
            rows = payload.get("data") or []
        txid: Optional[int] = None
        if rows and isinstance(rows[0], dict):
            txid = rows[0].get("txid")
        return {"txid": txid}

    async def get_transaction(
        self, ledger: str, txid: int
    ) -> dict[str, Any]:
        """Fetch a transaction by its numeric ID (normalised shape)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/{ledger}/transactions/{txid}"
            )
            resp.raise_for_status()
            payload = resp.json()

        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return _normalise_transaction(payload["data"])
        return payload

    async def list_transactions(
        self,
        ledger: str,
        page_size: int = 50,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        """List transactions with cursor-based pagination (normalised).

        ``after`` is the opaque ``cursor.next`` value from the previous page;
        on the real API it is sent as the ``cursor`` query param (the legacy ``after``
        param expects a numeric txid and 500s otherwise); the client omits
        pageSize on follow-ups.
        """
        if after:
            params: dict[str, Any] = {"cursor": after}
        else:
            params = {"pageSize": page_size}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/{ledger}/transactions",
                params=params,
            )
            resp.raise_for_status()
            payload = resp.json()

        cursor, rows = _cursor_data(payload)
        data = [
            _normalise_transaction(row) for row in rows if isinstance(row, dict)
        ]
        return {
            "cursor": {
                "hasMore": bool(cursor.get("hasMore")),
                "next": cursor.get("next"),
                "pageSize": cursor.get("pageSize", page_size),
            },
            "data": data,
        }

    async def get_balances(
        self, ledger: str, address: Optional[str] = None
    ) -> dict[str, Any]:
        """Get account balances (normalised to per-address/per-asset maps).

        Requests the maximum page size so any realistic chart of accounts
        fits a single page (the default is 15).
        """
        params: dict[str, Any] = {"pageSize": 1000}
        if address:
            params["address"] = address

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/{ledger}/balances",
                params=params,
            )
            resp.raise_for_status()
            payload = resp.json()

        _, rows = _cursor_data(payload)
        data: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for addr, assets in row.items():
                balances: dict[str, str] = {}
                if isinstance(assets, dict):
                    for asset, value in assets.items():
                        balances[str(asset)] = _minor_to_major(value, asset)
                data.append(
                    {"address": str(addr), "balances": balances}
                )
        return {"data": data}

    async def health_check(self) -> bool:
        """Check if the Formance Ledger is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/_info")
                return resp.status_code == 200
        except Exception:
            return False

    async def create_ledger(self, ledger: str) -> bool:
        """Create a Formance v2 ledger (POST ``{base}/v2/{ledger}``).

        Tolerates the duplicate answers (400 ``LEDGER_ALREADY_EXISTS`` on
        v2.3.x, 409 on older versions) and connection failures with a logged
        warning — never raises.  Returns True when the ledger exists
        (created now or already present), False when Formance is unreachable
        (the ledger can be created later).
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self.base_url}/v2/{ledger}")
            if resp.status_code in _CREATE_LEDGER_DUPLICATE:
                logger.warning(
                    "Formance ledger '%s' already exists (%d) — continuing",
                    ledger,
                    resp.status_code,
                )
                return True
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(
                "Failed to create Formance ledger '%s': %s — continuing "
                "(Formance may be started later)",
                ledger,
                exc,
            )
            return False


# Singleton instance
formance_client = FormanceClient()
