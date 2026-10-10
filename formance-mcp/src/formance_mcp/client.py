"""Self-contained httpx client for the Formance Ledger HTTP API.

This module owns its httpx client and never imports from ``api/``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "http://localhost:3068"
MAX_PAGINATION_PAGES = 1000


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
        """POST /{ledger}/transactions with the Idempotency-Key header when given."""
        body: dict[str, Any] = {"postings": postings}
        if reference:
            body["reference"] = reference
        if timestamp:
            body["timestamp"] = timestamp
        if metadata:
            body["metadata"] = metadata
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await self._request_json(
            "POST", f"/{ledger}/transactions", json=body, headers=headers
        )

    async def get_transaction(self, ledger: str, txid: int | str) -> dict[str, Any]:
        """GET /{ledger}/transactions/{txid}."""
        return await self._request_json("GET", f"/{ledger}/transactions/{txid}")

    async def list_transactions_page(
        self,
        ledger: str,
        page_size: int = 100,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        """One page of GET /{ledger}/transactions with cursor params."""
        params: dict[str, Any] = {"pageSize": page_size}
        if after is not None:
            params["after"] = after
        return await self._request_json("GET", f"/{ledger}/transactions", params=params)

    async def list_transactions_all(
        self,
        ledger: str,
        page_size: int = 100,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        """Follow cursor.hasMore to collect every transaction page.

        Returns the F2-compatible shape ``{"data": [...], "cursor": {"hasMore":
        false, "next": null}}`` once pagination is exhausted.
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

        Returns Formance's response untouched — per-address/per-asset
        structure is preserved, never merged.
        """
        params: dict[str, Any] = {}
        if address:
            params["address"] = address
        return await self._request_json("GET", f"/{ledger}/balances", params=params)

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
