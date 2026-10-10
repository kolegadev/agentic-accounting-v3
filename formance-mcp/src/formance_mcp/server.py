"""MCP server exposing the Formance Ledger HTTP API as tools.

Built on the official ``mcp`` SDK (``MCPServer``, the v2 successor of
FastMCP).  Self-contained: only this package's own ``client.py`` talks to
Formance.
"""

from __future__ import annotations

import functools
import os
import re
from typing import Any, Awaitable, Callable, Optional, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

from . import __version__
from .client import FormanceClient, FormanceError

__all__ = [
    "AMOUNT_RE",
    "FormanceToolError",
    "create_server",
    "get_client",
    "get_transport",
    "main",
    "mcp",
]

AMOUNT_PATTERN = r"^\d+(\.\d{1,2})?$"
AMOUNT_RE = re.compile(AMOUNT_PATTERN)

SUPPORTED_TRANSPORTS = ("stdio", "streamable-http")

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


class FormanceToolError(MCPToolError):
    """Structured tool error carrying the upstream Formance status and body.

    Raising this from a tool surfaces as ``isError: true`` on the wire with
    the full message (status + body) in the result content.
    """

    def __init__(
        self, message: str, *, status: Optional[int] = None, body: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _map_errors(fn: _F) -> _F:
    """Convert FormanceError into a structured FormanceToolError."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except FormanceToolError:
            raise
        except FormanceError as exc:
            raise FormanceToolError(
                str(exc), status=exc.status, body=exc.body
            ) from exc

    return wrapper  # type: ignore[return-value]


_client: Optional[FormanceClient] = None


def get_client() -> FormanceClient:
    """Return the process-wide Formance client, created lazily from env config."""
    global _client
    if _client is None:
        _client = FormanceClient()
    return _client


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_amount(amount: Any, where: str) -> None:
    if not isinstance(amount, str):
        raise FormanceToolError(
            f"{where}: amount must be a decimal string matching "
            f"{AMOUNT_PATTERN}; got {type(amount).__name__} — never pass floats"
        )
    if not AMOUNT_RE.fullmatch(amount):
        raise FormanceToolError(
            f"{where}: invalid amount {amount!r}; amounts must be decimal "
            f"strings matching {AMOUNT_PATTERN} (at most 2 decimal places)"
        )


def _validate_postings(postings: Any) -> None:
    if not isinstance(postings, list) or not postings:
        raise FormanceToolError("postings must be a non-empty list of posting objects")
    for index, posting in enumerate(postings):
        where = f"postings[{index}]"
        if not isinstance(posting, dict):
            raise FormanceToolError(
                f"{where}: must be an object with 'source', 'destination', "
                "'amount' and 'asset'"
            )
        for field in ("source", "destination", "asset"):
            if posting.get(field) is None:
                raise FormanceToolError(f"{where}: missing required field {field!r}")
        _validate_amount(posting.get("amount"), f"{where}.amount")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@_map_errors
async def health() -> dict[str, Any]:
    """Check Formance ledger health.

    Fetches Formance ``_info`` and reports service status plus version.
    """
    info = await get_client().info()
    version = info.get("version") if isinstance(info, dict) else None
    return {"status": "ok", "version": version, "info": info}


@_map_errors
async def create_transaction(
    ledger: str,
    postings: list[dict[str, Any]],
    reference: Optional[str] = None,
    timestamp: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create and commit a transaction in a ledger.

    ``postings`` is a list of objects, each with ``source``, ``destination``,
    ``amount`` (a DECIMAL STRING like "12.34" — floats are rejected) and
    ``asset``.  When ``idempotency_key`` is provided it is sent as the
    ``Idempotency-Key`` header.  Returns the committed transaction as
    reported by Formance (includes the txid).
    """
    _validate_postings(postings)
    return await get_client().create_transaction(
        ledger,
        postings,
        reference=reference,
        timestamp=timestamp,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )


@_map_errors
async def get_transaction(ledger: str, txid: int | str) -> dict[str, Any]:
    """Fetch a single committed transaction by numeric id."""
    return await get_client().get_transaction(ledger, txid)


@_map_errors
async def list_transactions(
    ledger: str, page_size: int = 100, after: Optional[str] = None
) -> dict[str, Any]:
    """List transactions in a ledger, following cursor pagination to the end.

    Returns ``{"data": [...], "cursor": {"hasMore": false, "next": null}}``
    after exhausting every page.  Pass ``after`` to resume from a cursor.
    """
    return await get_client().list_transactions_all(
        ledger, page_size=page_size, after=after
    )


@_map_errors
async def get_balances(
    ledger: str, address: Optional[str] = None
) -> dict[str, Any]:
    """Get account balances for a ledger, optionally filtered by ``address``.

    Preserves Formance's per-address/per-asset structure — no lossy merging.
    """
    return await get_client().get_balances(ledger, address=address)


@_map_errors
async def list_ledgers() -> dict[str, Any]:
    """List the ledgers known to the Formance instance.

    Reads GET /_info and tolerates GET / with a JSON listing when available.
    Returns ``{"info": ..., "ledgers": [...]}``.
    """
    return await get_client().list_ledgers()


@_map_errors
async def run_script(ledger: str, plain: str) -> dict[str, Any]:
    """Execute Numscript against a ledger: POST /{ledger}/script with body {plain}.

    Returns Formance's script result, or a structured tool error when the
    script fails.
    """
    return await get_client().run_script(ledger, plain)


# ---------------------------------------------------------------------------
# Server assembly and entrypoint
# ---------------------------------------------------------------------------


def _register_tools(server: MCPServer) -> None:
    server.tool()(health)
    server.tool()(create_transaction)
    server.tool()(get_transaction)
    server.tool()(list_transactions)
    server.tool()(get_balances)
    server.tool()(list_ledgers)
    server.tool()(run_script)


def create_server() -> MCPServer:
    """Build a fresh MCPServer with all Formance tools registered."""
    server = MCPServer(
        name="formance-ledger",
        title="Formance Ledger MCP",
        description="MCP server in front of the Formance Ledger HTTP API",
        version=__version__,
    )
    _register_tools(server)
    return server


mcp = create_server()


def get_transport() -> str:
    return os.getenv("MCP_TRANSPORT", "stdio").strip().lower()


def main() -> None:
    transport = get_transport()
    if transport not in SUPPORTED_TRANSPORTS:
        raise SystemExit(
            f"unsupported MCP_TRANSPORT {transport!r}; "
            f"expected 'stdio' or 'streamable-http'"
        )
    if transport == "stdio":
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport, host=os.getenv("MCP_HOST", "127.0.0.1"))


if __name__ == "__main__":
    main()
