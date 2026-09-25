"""The web API: streaming chat, per-visitor isolation, spend guards and the human-only approval queue.

The model is the scripted FakeClient from test_agent, so these tests make no API calls. The tools are
real, so the drafts and SQL come from the actual warehouse.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from stockroom import appdb, config
from stockroom.agent.loop import Agent
from stockroom.api.app import Settings, create_app
from tests.test_agent import FakeClient, resp, text, tool_use, usage

pytestmark = pytest.mark.skipif(not config.WAREHOUSE_DB.exists(), reason="run `make data` first")
needs_forecast = pytest.mark.skipif(not config.FORECAST_DB.exists(), reason="run `make forecast` first")


@pytest.fixture(autouse=True)
def temp_app(tmp_path, monkeypatch):
    appdb.close_all()
    monkeypatch.setattr(appdb, "APP_PATH", tmp_path / "app.duckdb")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    yield
    appdb.close_all()


def sid() -> str:
    return uuid.uuid4().hex


SQL_TURN = [
    resp([tool_use("run_sql", {"query": "SELECT store, count(*) AS n FROM core.dim_store GROUP BY 1 ORDER BY 1"}, 1)]),
    resp([text("There are 4 stores.")], stop="end_turn", u=usage(20_000, 500)),
]  # fmt: skip
DRAFT_TURN = [
    resp([tool_use("draft_reorder", {"store": "CA_4", "dept": "HOBBIES_2"}, 1)]),
    resp([text("Drafted; a buyer must approve.")], stop="end_turn"),
]


def make_client(script_per_conversation: list[list[Any]] | None = None, **settings: Any) -> TestClient:
    scripts = list(script_per_conversation or [])

    def factory(st: Settings) -> Agent:
        script = scripts.pop(0) if scripts else [resp([text("ok")], stop="end_turn")]
        return Agent(
            model="claude-sonnet-5", client=FakeClient(script), max_cost_usd=st.conversation_budget_usd
        )

    st = Settings(static_dir=None, **settings)
    return TestClient(create_app(st, agent_factory=factory))


def chat(c: TestClient, session: str, message: str, cid: str | None = None) -> list[tuple[str, dict]]:
    body = {"message": message, **({"conversation_id": cid} if cid else {})}
    with c.stream("POST", "/api/chat", json=body, headers={"X-Session-Id": session}) as r:
        assert r.status_code == 200, r.read()
        raw = "".join(r.iter_text())
    events = []
    for block in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


# ---- chat ----------------------------------------------------------------------------------------
def test_chat_streams_tool_progress_then_the_answer():
    c = make_client([SQL_TURN])
    ev = chat(c, sid(), "How many stores?")
    assert [e for e, _ in ev] == ["start", "tool_call", "tool_result", "answer", "done"]
    result = ev[2][1]
    assert result["name"] == "run_sql" and not result["is_error"]
    assert result["table"]["rows"] == [["CA_1", 1], ["CA_2", 1], ["CA_3", 1], ["CA_4", 1]]
    assert "core.dim_store" in result["sql"]
    assert ev[3][1]["text"] == "There are 4 stores." and ev[3][1]["tool_calls"] == 1


def test_tool_caveats_reach_the_trace_panel():
    script = [
        resp([tool_use("run_sql", {"query": "SELECT sum(units) FROM core.fact_sales_daily WHERE store = 'CA_4'"}, 1)]),
        resp([text("done")], stop="end_turn"),
    ]  # fmt: skip
    ev = chat(make_client([script]), sid(), "CA_4 units?")
    assert any("CA_4 2016-03-14" in c for c in ev[2][1]["caveats"])


def test_conversations_continue_but_only_for_their_owner():
    c = make_client([[resp([text("first")], stop="end_turn"), resp([text("second")], stop="end_turn")]])
    me = sid()
    cid = chat(c, me, "hi")[0][1]["conversation_id"]
    assert chat(c, me, "again", cid)[1][1]["text"] == "second"
    r = c.post("/api/chat", json={"message": "x", "conversation_id": cid}, headers={"X-Session-Id": sid()})
    assert r.status_code == 404


def test_session_header_is_required_and_validated():
    c = make_client()
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 400
    assert c.post("/api/chat", json={"message": "hi"}, headers={"X-Session-Id": "../etc"}).status_code == 400
    assert c.get("/api/drafts").status_code == 400


def test_long_messages_are_refused():
    r = make_client().post("/api/chat", json={"message": "x" * 1001}, headers={"X-Session-Id": sid()})
    assert r.status_code == 413


# ---- spend guards -----------------------------------------------------------------------------------
def test_daily_budget_counts_real_cost_and_then_closes_chat():
    c = make_client([SQL_TURN], daily_budget_usd=0.01)
    assert c.get("/api/meta").json()["chat_enabled"]
    chat(c, sid(), "How many stores?")  # costs more than $0.01 with the scripted token usage
    m = c.get("/api/meta").json()
    assert m["spent_today_usd"] > 0.01 and not m["chat_enabled"] and "budget" in m["chat_disabled_reason"]
    r = c.post("/api/chat", json={"message": "again"}, headers={"X-Session-Id": sid()})
    assert r.status_code == 503 and "00:00 UTC" in r.json()["error"]


def test_questions_per_hour_limit_per_visitor():
    c = make_client(questions_per_hour=2)
    me = sid()
    chat(c, me, "1")
    chat(c, me, "2")
    r = c.post("/api/chat", json={"message": "3"}, headers={"X-Session-Id": me})
    assert r.status_code == 429 and "per hour" in r.json()["error"]


def test_forwarded_ip_is_trusted_only_when_configured():
    c = make_client(questions_per_hour=1, trust_proxy=True)
    chat_ok = c.post(
        "/api/chat", json={"message": "a"}, headers={"X-Session-Id": sid(), "X-Forwarded-For": "1.1.1.1"}
    )
    chat_other = c.post(
        "/api/chat", json={"message": "b"}, headers={"X-Session-Id": sid(), "X-Forwarded-For": "2.2.2.2"}
    )
    assert chat_ok.status_code == chat_other.status_code == 200  # different visitors behind the proxy


def test_without_an_api_key_chat_is_off_and_says_why(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(create_app(Settings(static_dir=None)))
    m = c.get("/api/meta").json()
    assert not m["chat_enabled"] and "no API key" in m["chat_disabled_reason"]
    assert m["eval"]["configs"]["sonnet"]["overall"]["acc"] == 100.0  # recorded results still shown


def test_a_slot_is_released_after_each_turn():
    c = make_client(max_concurrent=1)
    for i in range(3):
        assert chat(c, sid(), f"q{i}")[-1][0] == "done"


# ---- the approval queue: human-only, per visitor -------------------------------------------------------
@needs_forecast
def test_drafts_are_per_visitor_and_only_a_named_human_decides():
    c = make_client([DRAFT_TURN])
    me, other = sid(), sid()
    ev = chat(c, me, "Draft a reorder for CA_4 HOBBIES_2")
    assert ev[2][1]["name"] == "draft_reorder"
    mine = c.get("/api/drafts", headers={"X-Session-Id": me}).json()
    assert mine and {d["status"] for d in mine} == {"PENDING_APPROVAL"}
    assert c.get("/api/drafts", headers={"X-Session-Id": other}).json() == []

    did = mine[0]["draft_id"]
    assert c.get(f"/api/drafts/{did}", headers={"X-Session-Id": me}).json()["lines"]
    assert c.get(f"/api/drafts/{did}", headers={"X-Session-Id": other}).status_code == 404
    decide = lambda s, body: c.post(f"/api/drafts/{did}/decision", json=body, headers={"X-Session-Id": s})  # noqa: E731
    assert decide(other, {"decision": "approve", "by": "Mallory"}).status_code == 404
    assert decide(me, {"decision": "approve", "by": "  "}).status_code == 400
    assert decide(me, {"decision": "reject", "by": "Aziz"}).status_code == 400  # a rejection needs a reason
    ok = decide(me, {"decision": "approve", "by": "Aziz", "note": "looks right"})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    assert decide(me, {"decision": "reject", "by": "Aziz", "note": "changed my mind"}).status_code == 400
    assert c.get("/api/drafts", headers={"X-Session-Id": me}).json()[0]["decided_by"] == "Aziz"


def test_the_agent_cannot_reach_the_decision_route():
    """The decision route is HTTP-only; the agent's only capabilities are the registry tools."""
    from stockroom.tools import TOOLS  # noqa: PLC0415

    c = make_client()
    routes = {r.path for r in c.app.routes}
    assert "/api/drafts/{draft_id}/decision" in routes
    assert not any(v in name for name in TOOLS for v in ("approve", "decide", "decision", "send"))


# ---- static UI ------------------------------------------------------------------------------------------
def test_serves_the_static_ui(tmp_path):
    (tmp_path / "index.html").write_text("<html>stockroom</html>")
    c = TestClient(create_app(Settings(static_dir=tmp_path), agent_factory=lambda st: None))
    assert "stockroom" in c.get("/").text
    assert c.get("/api/health").json()["ok"]
    assert c.get("/").headers["x-content-type-options"] == "nosniff"
