"""Stockroom's tools as an MCP server, for Claude Desktop, Claude Code or any MCP client.

    stockroom-mcp                      # stdio (what Claude Desktop and Claude Code launch)
    stockroom-mcp --http [--port N]    # streamable HTTP on 127.0.0.1:N/mcp

A thin adapter over the registry (ADR 0006):
- Tool names, descriptions and JSON schemas come from `stockroom.tools.TOOLS`, the same definitions
  the P5 eval measured. Nothing is re-derived from Python signatures, so the two front doors can't drift.
- Every call goes through `stockroom.tools.call`, so the SQL guard, argument checks and drafts-only
  rule apply unchanged. The eval-only raw-data mode is not reachable: this adapter never passes `schema`.
- Results are the {caveats, data, provenance} envelope as JSON, caveats first, so a client that
  truncates long tool output cuts rows, not the warning that a day's sales are unknown.
- Each call is appended to runs/mcp/calls.jsonl (tool, arguments, client, time, error). Nothing is
  written to stdout, which carries the stdio protocol.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from stockroom import config
from stockroom.agent.prompts import FIRM, RULES
from stockroom.tools import TOOLS, call
from stockroom.tools.base import as_of

log = logging.getLogger("stockroom.mcp")
AUDIT_LOG: Path = config.RUNS_DIR / "mcp" / "calls.jsonl"
DEFAULT_PORT = 8765

TITLES = {
    "describe_data": "Describe the warehouse",
    "run_sql": "Run a read-only SQL query",
    "detect_anomalies": "Find stock-outs, overstock and sales anomalies",
    "explain_variance": "Explain a revenue change",
    "forecast_demand": "Forecast SKU demand (28 days)",
    "draft_reorder": "Draft purchase orders (for human approval)",
}
WRITES = {"draft_reorder"}  # inserts PENDING_APPROVAL drafts into app.duckdb; everything else only reads


def instructions() -> str:
    return (
        "Stockroom's tools answer operations questions about " + FIRM.format(as_of=as_of()) + "\n\n" + RULES
    )


def mcp_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=t.name,
            title=TITLES[t.name],
            description=t.description,
            inputSchema=t.input_schema,
            annotations=types.ToolAnnotations(
                title=TITLES[t.name],
                readOnlyHint=t.name not in WRITES,
                destructiveHint=False,  # drafts are additive; nothing is updated, deleted or sent
                idempotentHint=t.name not in WRITES,
                openWorldHint=False,  # a closed dataset; no external systems
            ),
        )
        for t in TOOLS.values()
    ]


def envelope_text(result: dict[str, Any]) -> str:
    """Caveats first: if a client truncates long output, the data-quality warnings survive."""
    ordered = {k: result[k] for k in ("error", "caveats", "data", "provenance") if k in result}
    ordered |= {k: v for k, v in result.items() if k not in ordered}
    return json.dumps(ordered, separators=(",", ":"), default=str)


def _audit(entry: dict[str, Any]) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:  # the audit log must never break a tool call
        log.warning("could not write audit log: %s", e)


def build_server() -> Server:
    server: Server = Server("stockroom", version="0.1.0", instructions=instructions())
    tools = mcp_tools()

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()  # validates arguments against the registry schema before we see them
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        client = None
        with contextlib.suppress(Exception):
            info = server.request_context.session.client_params.clientInfo
            client = f"{info.name}/{info.version}"
        t0 = time.perf_counter()
        result = await anyio.to_thread.run_sync(lambda: call(name, arguments))
        _audit(
            {
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "client": client,
                "tool": name,
                "arguments": arguments,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": result.get("error"),
                "caveats": len(result.get("caveats", [])),
            }
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=envelope_text(result))],
            isError="error" in result,
        )

    return server


async def serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_app(host: str = "127.0.0.1", port: int = DEFAULT_PORT):
    """Streamable HTTP at /mcp. Loopback only: the server has no authentication (P7 adds a real API)."""
    if not is_loopback(host):
        raise ValueError(
            f"refusing to listen on {host}: this server has no authentication, so it binds to loopback only"
        )
    manager = StreamableHTTPSessionManager(
        app=build_server(),
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,  # a web page can't reach it through a rebound hostname
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="stockroom-mcp", description=__doc__.split("\n")[0])
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if not config.WAREHOUSE_DB.exists():
        sys.exit(f"stockroom-mcp: {config.WAREHOUSE_DB} not found. Run `make data` first.")
    if a.http:
        try:
            app = http_app(a.host, a.port)
        except ValueError as e:
            sys.exit(f"stockroom-mcp: {e}")
        print(f"stockroom-mcp: http://{a.host}:{a.port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    else:
        anyio.run(serve_stdio)


if __name__ == "__main__":
    main()
