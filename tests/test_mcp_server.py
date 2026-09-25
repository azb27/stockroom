"""The MCP server is a faithful, no-extra-powers adapter over the tool registry.

Parity with `stockroom.tools.call` is what lets the P5 eval numbers carry over to Claude Desktop and
Claude Code: same schemas, same results, same guards.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import tomllib

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from stockroom import appdb, config, mcp_server
from stockroom.tools import TOOLS, call

pytestmark = [
    pytest.mark.skipif(not config.WAREHOUSE_DB.exists(), reason="run `make data` first"),
    pytest.mark.anyio,
]
needs_forecast = pytest.mark.skipif(not config.FORECAST_DB.exists(), reason="run `make forecast` first")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def audit_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "AUDIT_LOG", tmp_path / "calls.jsonl")


async def _call(name: str, args: dict | None = None):
    async with create_connected_server_and_client_session(mcp_server.build_server()) as s:
        r = await s.call_tool(name, args or {})
    return r, json.loads(r.content[0].text) if r.content and r.content[0].text.startswith("{") else None


def _strip_timing(d: dict) -> dict:
    d = json.loads(json.dumps(d))
    d.get("provenance", {}).pop("tool_ms", None)
    d.get("provenance", {}).pop("elapsed_ms", None)
    return d


# ---- the catalogue is the registry, nothing more ---------------------------------------------------
async def test_tools_are_exactly_the_registry():
    async with create_connected_server_and_client_session(mcp_server.build_server()) as s:
        listed = (await s.list_tools()).tools
    assert [t.name for t in listed] == list(TOOLS)
    for t in listed:
        assert t.inputSchema == TOOLS[t.name].input_schema  # same schemas the eval measured
        assert t.description == TOOLS[t.name].description


async def test_no_tool_can_approve_or_send_and_only_drafting_writes():
    async with create_connected_server_and_client_session(mcp_server.build_server()) as s:
        listed = (await s.list_tools()).tools
    names = " ".join(t.name for t in listed).lower()
    assert not any(v in names for v in ("approve", "send", "submit", "place", "decide"))
    writers = {t.name for t in listed if not t.annotations.readOnlyHint}
    assert writers == {"draft_reorder"}
    assert all(
        t.annotations.destructiveHint is False and t.annotations.openWorldHint is False for t in listed
    )


async def test_server_instructions_carry_the_agent_rules():
    async with create_connected_server_and_client_session(mcp_server.build_server()) as s:
        init = await s.initialize()
    text = init.instructions
    assert "missing_data is UNKNOWN" in text and "cannot approve" in text and "2016-05-22" in text


# ---- same results as calling the library directly -------------------------------------------------
PARITY = [
    ("describe_data", {}),
    ("run_sql", {"query": "SELECT store, sum(units) AS u FROM core.fact_sales_daily "
                          "WHERE date BETWEEN '2016-03-10' AND '2016-03-20' GROUP BY 1 ORDER BY 1"}),
    ("detect_anomalies", {"scope": "inventory", "store": "CA_2", "limit": 5}),
    ("explain_variance", {"period_a_start": "2016-04-01", "period_a_end": "2016-04-14",
                          "period_b_start": "2016-04-15", "period_b_end": "2016-04-28", "by": "dept"}),
    ("run_sql", {"query": "SELECT count(*) FROM core.dim_sku WHERE status = 'active'"}),  # empty-result hint
]  # fmt: skip


@pytest.mark.parametrize(("name", "args"), PARITY, ids=[f"{n}-{i}" for i, (n, _) in enumerate(PARITY)])
async def test_results_match_the_library(name, args):
    r, got = await _call(name, args)
    assert not r.isError
    assert _strip_timing(got) == _strip_timing(call(name, args))


@needs_forecast
async def test_forecast_matches_the_library():
    args = {"sku": "foods-3-090", "store": "CA_1", "horizon_days": 7}  # legacy spelling resolves
    _, got = await _call("forecast_demand", args)
    assert _strip_timing(got) == _strip_timing(call("forecast_demand", args))


async def test_caveats_come_before_data():
    r, _ = await _call(
        "run_sql", {"query": "SELECT sum(units) FROM core.fact_sales_daily WHERE store = 'CA_4'"}
    )
    text = r.content[0].text
    assert "CA_4 2016-03-14" in text and text.index('"caveats"') < text.index('"data"')


# ---- guards hold through the MCP door -----------------------------------------------------------------
async def test_writes_and_raw_tables_are_rejected():
    for q in ("DELETE FROM core.dim_sku", "SELECT * FROM raw.pos_sales_lines", "ATTACH 'x.db'"):
        r, body = await _call("run_sql", {"query": q})
        assert r.isError and body["error"].startswith("Query rejected")


async def test_undeclared_arguments_are_rejected_so_raw_mode_is_unreachable():
    r, _ = await _call("run_sql", {"query": "SELECT 1", "schema": "raw"})
    assert r.isError and "Additional properties" in r.content[0].text
    r, _ = await _call("describe_data", {"allowed_schemas": ["raw"]})
    assert r.isError


async def test_unknown_tool_is_an_error():
    r, _ = await _call("approve_po", {"draft_id": "PO-1"})
    assert r.isError


async def test_every_call_is_audited():
    await _call("run_sql", {"query": "SELECT 1 AS one"})
    await _call("run_sql", {"query": "DROP TABLE core.dim_sku"})
    rows = [json.loads(line) for line in mcp_server.AUDIT_LOG.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["run_sql", "run_sql"]
    assert rows[0]["error"] is None and rows[1]["error"].startswith("Query rejected")
    assert rows[0]["client"]  # which MCP client made the call


@needs_forecast
async def test_drafts_through_mcp_are_pending(tmp_path, monkeypatch):
    appdb.close_all()
    monkeypatch.setattr(appdb, "APP_PATH", tmp_path / "app.duckdb")
    r, body = await _call("draft_reorder", {"store": "CA_4", "dept": "HOBBIES_2"})
    assert not r.isError and body["data"]["drafts"]
    with appdb.app_session() as con:
        assert {s for (s,) in con.execute("SELECT DISTINCT status FROM po_drafts").fetchall()} == {
            "PENDING_APPROVAL"
        }
    appdb.close_all()


# ---- the real transports ------------------------------------------------------------------------------
async def test_stdio_entry_point_serves_tools():
    """What Claude Desktop and Claude Code do: spawn the process and talk JSON-RPC over stdio."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "stockroom.mcp_server"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        assert len((await s.list_tools()).tools) == len(TOOLS)
        r = await s.call_tool("run_sql", {"query": "SELECT count(*) AS n FROM core.dim_store"})
    assert json.loads(r.content[0].text)["data"]["rows"] == [[4]]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "stockroom.mcp_server", "--http", "--port", str(port)],
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


async def test_streamable_http_serves_tools(http_server):
    async with (
        streamable_http_client(f"http://127.0.0.1:{http_server}/mcp") as (read, write, _),
        ClientSession(read, write) as s,
    ):
        await s.initialize()
        r = await s.call_tool("describe_data", {})
    assert "core.sales_enriched" in json.loads(r.content[0].text)["data"]["tables"]


def test_http_rejects_a_foreign_host_header(http_server):
    """DNS-rebinding protection: a page on evil.example that resolves to 127.0.0.1 can't drive the server."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Host": "evil.example", "Accept": "application/json, text/event-stream"}
    r = httpx.post(f"http://127.0.0.1:{http_server}/mcp/", json=body, headers=headers)
    assert r.status_code in (400, 403, 421)


def test_http_refuses_to_bind_beyond_loopback():
    with pytest.raises(SystemExit, match="loopback only"):
        mcp_server.main(["--http", "--host", "0.0.0.0"])
    assert mcp_server.is_loopback("127.0.0.1") and mcp_server.is_loopback("::1")
    assert not mcp_server.is_loopback("192.168.1.10")


# ---- the Claude Code wiring ------------------------------------------------------------------------------
def test_mcp_json_launches_the_declared_entry_point():
    servers = json.loads((config.ROOT / ".mcp.json").read_text())["mcpServers"]
    scripts = tomllib.loads((config.ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert servers["stockroom"]["command"] in scripts


def test_skill_preapproves_only_read_only_stockroom_tools():
    text = (config.ROOT / ".claude" / "skills" / "stockroom-analyst" / "SKILL.md").read_text()
    front = text.split("---")[1]
    allowed = next(line for line in front.splitlines() if line.startswith("allowed-tools:"))
    tools = {t.strip() for t in allowed.split(":", 1)[1].split(",")}
    assert tools == {f"mcp__stockroom__{n}" for n in TOOLS if n not in mcp_server.WRITES}
    assert "name: stockroom-analyst" in front and "description:" in front
