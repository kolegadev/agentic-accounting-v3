"""Self-contained httpx client for the Formance Ledger HTTP API.

This module owns its httpx client and never imports from ``api/``.

The wire format mirrors the F14 API client (``api/src/services/formance_client.py``)
exactly, observed against ``ghcr.io/formancehq/ledger`` v2.3.21:

- ``create_transaction``: amounts are minor-unit integers on the wire and the
  engine rejects any posting that would make a non-``world`` account negative
  (``INSUFFICIENT_FUND``).  Transactions are therefore submitted as Numscript
  with ``allowing unbounded overdraft`` on every source — one ``send`` per
  posting — so credit-nature accounts hold negative balances exactly as
  double-entry bookkeeping requires.  Amounts arrive here as decimal strings
  (the MCP tool's public shape) and are converted to minor-unit integers
  EXACTLY (``Decimal * 100 -> int``, never floats).
- ``create_ledger``: ``POST /v2/{ledger}`` answers 204 for a new ledger and
  400 ``LEDGER_ALREADY_EXISTS`` for a repeat (the old 409 is also tolerated).
- ``list_transactions`` / ``get_balances``: the real envelope embeds the data
  list INSIDE ``cursor`` and reports amounts as minor-unit integers; this
  client normalises to ``{"cursor": {...}, "data": [...]}`` with major-unit
  decimal strings.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "http://localhost:3068"
MAX_PAGINATION_PAGES = 1000

# Real create-ledger answer codes: 200/201/204 = created (or already
# created), 400 LEDGER_ALREADY_EXISTS / 409 = duplicate — all idempotent
# successes for our purposes.
_CREATE_LEDGER_OK = (200, 201, 204)
_CREATE_LEDGER_DUPLICATE = (400, 409)


class FormanceError(Exception):
    """Structured error from the Formance API or its transport.

    ``status`` is the HTTP status (``None`` for connection failures) and
    ``body`` the decoded JSON body when available.  The message always
    includes the status and body so MCP clients see them in ``isError``
    results.
    """

    def __init__(
        self, message: str, *, status: Optional[int] = None, body: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _ledger_name(item: Any) -> Optional[str]:
    if isinstance(item, dict):
        name = item.get("name")
        return str(name) if name is not None else None
    if isinstance(item, str):
        return item
    return None


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
    (never silently truncated).  Uses ``Decimal`` only — no floats.
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
    """Async HTTP client for the Formance ledger API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        base = base_url or os.getenv("FORNANCE_LEDGER_URL", DEFAULT_BASE_URL)
        self.base_url = base.rstrip("/")
        self.auth_token = (
            auth_token if auth_token is not None else os.getenv("FORMANCE_AUTH_TOKEN")
        )
        self.timeout = timeout
        self._transport = transport

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if extra:
            headers.update(extra)
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                return await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json,
                    headers=self._headers(headers),
                )
        except httpx.HTTPError as exc:
            raise FormanceError(
                f"Formance connection failure: {exc.__class__.__name__}: {exc}"
            ) from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        resp = await self._send(
            method, path, params=params, json=json, headers=headers
        )
        if resp.status_code >= 400:
            body: Any = None
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:2000] or None
            raise FormanceError(
                f"Formance HTTP {resp.status_code}: {body}",
                status=resp.status_code,
                body=body,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise FormanceError(
                f"Formance HTTP {resp.status_code}: response was not JSON",
                status=resp.status_code,
                body=resp.text[:2000] or None,
            ) from exc

    async def info(self) -> Any:
        """GET /_info — service metadata (version, service name, ...)."""
        return await self._request_json("GET", "/_info")

    async def create_transaction(
        self,
        ledger: str,
        postings: list[dict[str, Any]],
        *,
        reference: Optional[str] = None,
        timestamp: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /{ledger}/transactions — Numscript, one ``send`` per posting.

        Amounts arrive as decimal strings (the MCP tool's public shape) and
        are converted to minor-unit integers exactly.  The ``Idempotency-Key``
        header is sent when given.  The real response envelope
        ``{"data": [{"txid": N, ...}]}`` is collapsed to ``{"txid": N}``.
        """
        body: dict[str, Any] = {}
        try:
            body = {"script": {"plain": _build_numscript(postings)}}
        except ValueError as exc:
            raise FormanceError(f"invalid posting amounts: {exc}") from exc
        if reference:
            body["reference"] = reference
        if timestamp:
            body["timestamp"] = timestamp
        if metadata:
            body["metadata"] = metadata
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        payload = await self._request_json(
            "POST", f"/{ledger}/transactions", json=body, headers=headers
        )
        rows: list[Any] = []
        if isinstance(payload, dict):
            rows = payload.get("data") or []
        txid: Optional[int] = None
        if rows and isinstance(rows[0], dict):
            txid = rows[0].get("txid")
        return {"txid": txid}

    async def get_transaction(self, ledger: str, txid: int | str) -> dict[str, Any]:
        """GET /{ledger}/transactions/{txid}.

        The transaction inside ``data`` is normalised to major-unit decimal
        string amounts; the ``{"data": {...}}`` envelope is preserved.
        """
        payload = await self._request_json("GET", f"/{ledger}/transactions/{txid}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return {"data": _normalise_transaction(payload["data"])}
        return payload

    async def list_transactions_page(
        self,
        ledger: str,
        page_size: int = 100,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        """One page of GET /{ledger}/transactions, normalised.

        ``after`` is the opaque ``cursor.next`` value from the previous page;
        on the real API it is sent as the ``cursor`` query param (v2.3.x 500s on the
        legacy ``after`` param, numeric or opaque alike; pageSize may accompany
        cursor but the client omits it on follow-ups).  The cursor-embedded envelope is flattened to
        ``{"cursor": {...}, "data": [...]}`` with major-unit amount strings.
        """
        if after:
            params: dict[str, Any] = {"cursor": after}
        else:
            params = {"pageSize": page_size}

        payload = await self._request_json(
            "GET", f"/{ledger}/transactions", params=params
        )
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

    async def list_transactions_all(
        self,
        ledger: str,
        page_size: int = 100,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        """Follow cursor.hasMore to collect every transaction page.

        Returns the F2-compatible shape ``{"data": [...], "cursor": {"hasMore":
        false, "next": null}}`` once pagination is exhausted.  Follow-up pages
        send the opaque ``cursor.next`` value without pageSize.
        """
        collected: list[Any] = []
        cursor_after: Optional[str] = after
        for _ in range(MAX_PAGINATION_PAGES):
            page = await self.list_transactions_page(
                ledger, page_size=page_size, after=cursor_after
            )
            if not isinstance(page, dict):
                break
            data = page.get("data") or []
            if isinstance(data, list):
                collected.extend(data)
            cursor = page.get("cursor") or {}
            has_more = bool(cursor.get("hasMore"))
            nxt = cursor.get("next")
            if nxt is None and isinstance(data, list) and data and isinstance(data[-1], dict):
                nxt = data[-1].get("txid")
            if not has_more or nxt is None or str(nxt) == cursor_after:
                break
            cursor_after = str(nxt)
        return {
            "data": collected,
            "cursor": {"hasMore": False, "next": None},
        }

    async def get_balances(
        self, ledger: str, address: Optional[str] = None
    ) -> dict[str, Any]:
        """GET /{ledger}/balances, optionally filtered by address.

        Requests the maximum page size so any realistic chart of accounts
        fits a single page (the default is 15).  The real envelope's
        ``{address: {asset: minor-int}}`` rows are normalised to
        ``{"data": [{"address": ..., "balances": {asset: major-str}}]}``.
        """
        params: dict[str, Any] = {"pageSize": 1000}
        if address:
            params["address"] = address

        payload = await self._request_json("GET", f"/{ledger}/balances", params=params)
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

    async def create_ledger(self, ledger: str) -> bool:
        """POST /v2/{ledger} — create a Formance v2 ledger.

        Tolerates the duplicate answers (400 ``LEDGER_ALREADY_EXISTS`` on
        v2.3.x, 409 on older versions) as idempotent success: returns True
        when the ledger exists (created now or already present).  Other
        4xx/5xx and connection failures raise a structured ``FormanceError``.
        """
        resp = await self._send("POST", f"/v2/{ledger}")
        if resp.status_code in _CREATE_LEDGER_OK:
            return True
        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code in _CREATE_LEDGER_DUPLICATE:
            if isinstance(body, dict) and body.get("errorCode") == "LEDGER_ALREADY_EXISTS":
                return True
        if body is None:
            body = resp.text[:2000] or None
        raise FormanceError(
            f"Formance HTTP {resp.status_code}: {body}",
            status=resp.status_code,
            body=body,
        )

    async def list_ledgers(self) -> dict[str, Any]:
        """Discover ledgers: GET /_info plus a tolerant GET / JSON listing.

        Raises a structured FormanceError only when no usable source is
        available at all (connection failure, or neither endpoint yields a
        listing).
        """
        result: dict[str, Any] = {"info": None, "ledgers": []}
        info: Any = None
        try:
            info = await self._request_json("GET", "/_info")
            result["info"] = info
        except FormanceError as exc:
            if exc.status is None:
                raise
        root: Any = None
        try:
            root = await self._request_json("GET", "/")
        except FormanceError as exc:
            if exc.status is None:
                raise
        names = self._extract_ledger_names(root)
        if names:
            result["ledgers"] = names
        if info is None and not names:
            raise FormanceError(
                "Formance list_ledgers: no ledger listing available (tried /_info and /)"
            )
        return result

    @staticmethod
    def _extract_ledger_names(root: Any) -> list[str]:
        names: list[str] = []
        if isinstance(root, list):
            for item in root:
                name = _ledger_name(item)
                if name:
                    names.append(name)
        elif isinstance(root, dict):
            for key in ("ledgers", "data"):
                value = root.get(key)
                if isinstance(value, list):
                    for item in value:
                        name = _ledger_name(item)
                        if name:
                            names.append(name)
                    break
            if not names and root.get("name"):
                names.append(str(root["name"]))
        return names

    async def run_script(self, ledger: str, plain: str) -> dict[str, Any]:
        """POST /{ledger}/script with Numscript body {"plain": plain}."""
        return await self._request_json(
            "POST", f"/{ledger}/script", json={"plain": plain}
        )
