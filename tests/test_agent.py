"""The agent loop's guarantees, tested offline with a scripted fake model (no API calls, no cost)."""

from __future__ import annotations

import json
from itertools import pairwise
from types import SimpleNamespace as NS
from typing import Any

import pytest

from stockroom import appdb, config
from stockroom.agent.loop import MAX_RESULT_CHARS, Agent, ConversationClosed, _tool_result_text
from stockroom.agent.prompts import system_prompt

needs_data = pytest.mark.skipif(not config.WAREHOUSE_DB.exists(), reason="run `make data` first")


def usage(inp: int = 1000, out: int = 100) -> NS:
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def tool_use(name: str, inp: dict[str, Any], i: int) -> NS:
    return NS(type="tool_use", name=name, input=inp, id=f"tu_{i}")


def text(t: str) -> NS:
    return NS(type="text", text=t)


class FakeClient:
    """Plays back a script of responses and records every request it received."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        step = self.script.pop(0) if self.script else None
        if callable(step):
            step = step(kw)
        return step


def resp(blocks, stop="tool_use", u=None) -> NS:
    return NS(content=blocks, stop_reason=stop, usage=u or usage())


@pytest.fixture(autouse=True)
def temp_app(tmp_path, monkeypatch):
    appdb.close_all()
    monkeypatch.setattr(appdb, "APP_PATH", tmp_path / "app.duckdb")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    yield
    appdb.close_all()


def fake_tools(name, args):
    return {"data": {"echo": args}, "caveats": [f"caveat from {name}"], "provenance": {}}


def assert_well_formed(messages: list[dict[str, Any]]) -> None:
    """Roles alternate and every tool_use is answered by a tool_result in the next message."""
    for a, b in pairwise(messages):
        assert a["role"] != b["role"]
    for i, m in enumerate(messages):
        if m["role"] == "assistant":
            ids = {x.id for x in m["content"] if getattr(x, "type", None) == "tool_use"}
            if ids:
                nxt = messages[i + 1]["content"]
                got = {
                    x["tool_use_id"] for x in nxt if isinstance(x, dict) and x.get("type") == "tool_result"
                }
                assert ids == got


def test_simple_turn_answers_traces_and_forwards_caveats():
    client = FakeClient([
        resp([tool_use("describe_data", {}, 1)]),
        resp([text("There are 4 stores.")], stop="end_turn"),
    ])  # fmt: skip
    a = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools)
    t = a.ask("How many stores?")
    assert t.answer == "There are 4 stores." and t.tool_calls == 1 and not t.limits_hit
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "caveat from describe_data" in result["content"]
    assert_well_formed(a.messages)
    with appdb.app_session() as con:
        row = con.execute("SELECT turn, tool_calls, answer FROM traces").fetchall()
    assert row == [(1, 1, "There are 4 stores.")]
    assert list((config.RUNS_DIR / "traces").glob("*.jsonl"))


def test_request_carries_system_prompt_tools_caching_and_effort():
    client = FakeClient([resp([text("hi")], stop="end_turn")])
    Agent(model="claude-sonnet-5", effort="low", client=client, tool_caller=fake_tools).ask("hi")
    r = client.requests[0]
    assert r["system"] == system_prompt() and "2016-05-22" in r["system"]
    assert r["cache_control"] == {"type": "ephemeral"} and r["output_config"] == {"effort": "low"}
    names = {t["name"] for t in r["tools"]}
    assert "draft_reorder" in names and not any(v in " ".join(names) for v in ("approve", "send"))
    assert "never say or imply that you have" in r["system"]


def test_tool_call_cap_forces_a_final_answer_without_tools():
    always_tool = lambda kw: (  # noqa: E731
        resp([text("done, partially")], stop="end_turn") if kw.get("tool_choice") == {"type": "none"}
        else resp([tool_use("run_sql", {"query": "SELECT 1"}, len(kw["messages"]))])
    )  # fmt: skip
    client = FakeClient([always_tool] * 10)
    a = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, max_tool_calls=3)
    t = a.ask("loop forever")
    assert t.tool_calls == 3 and t.answer == "done, partially"
    assert any("tool-call limit" in x for x in t.limits_hit)
    assert client.requests[-1]["tool_choice"] == {"type": "none"}
    assert_well_formed(a.messages)


def test_parallel_tool_calls_beyond_the_cap_get_error_results_not_silence():
    client = FakeClient([
        resp([tool_use("run_sql", {"query": f"SELECT {i}"}, i) for i in range(5)]),
        resp([text("ok")], stop="end_turn"),
    ])  # fmt: skip
    a = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, max_tool_calls=3)
    t = a.ask("five at once")
    results = [x for x in a.messages[2]["content"] if x.get("type") == "tool_result"]
    assert t.tool_calls == 3 and len(results) == 5
    assert sum(1 for r in results if r.get("is_error")) == 2
    assert_well_formed(a.messages)


def test_cost_cap_ends_turn_then_closes_conversation():
    big = usage(inp=300_000, out=10_000)  # ~$0.70 on Sonnet 5 pricing
    client = FakeClient([
        resp([tool_use("describe_data", {}, 1)], u=big),
        resp([text("stopping here")], stop="end_turn"),
    ])  # fmt: skip
    a = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, max_cost_usd=0.50)
    t = a.ask("expensive")
    assert any("cost limit" in x for x in t.limits_hit) and t.answer == "stopping here"
    assert client.requests[-1]["tool_choice"] == {"type": "none"}
    with pytest.raises(ConversationClosed):
        a.ask("another")


def test_time_limit_forces_final_answer():
    client = FakeClient([resp([tool_use("describe_data", {}, 1)]), resp([text("quick")], stop="end_turn")])
    t = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, max_seconds=0).ask("slow")
    assert any("time limit" in x for x in t.limits_hit)


@needs_data
def test_tool_errors_reach_the_model_as_recoverable_errors():
    client = FakeClient([
        resp([tool_use("run_sql", {"query": "SELECT * FROM raw.stores"}, 1)]),
        resp([tool_use("run_sql", {"query": "SELECT count(*) FROM core.dim_store"}, 2)]),
        resp([text("4 stores")], stop="end_turn"),
    ])  # fmt: skip
    a = Agent(model="claude-sonnet-5", client=client)  # real tools, real guard
    t = a.ask("stores?")
    first = a.messages[2]["content"][0]
    assert first["is_error"] and "Query rejected" in first["content"]
    assert [s.is_error for s in t.steps] == [True, False]


def test_multi_turn_conversation_keeps_history():
    client = FakeClient([resp([text("A1")], stop="end_turn"), resp([text("A2")], stop="end_turn")])
    a = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools)
    a.ask("Q1")
    a.ask("Q2")
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "assistant", "user"]
    assert a.turn == 2


def test_caveats_survive_truncation_of_huge_results():
    s = _tool_result_text(
        {"data": {"rows": ["x" * 100] * 1000}, "caveats": ["MISSING DATA for CA_4"], "provenance": {}}
    )
    assert len(s) < MAX_RESULT_CHARS + 100 and s.index("MISSING DATA") < 100 and "truncated" in s


def test_effort_only_sent_to_models_that_support_it():
    client = FakeClient([resp([text("hi")], stop="end_turn")])
    a = Agent(model="claude-haiku-4-5-20251001", effort="high", client=client, tool_caller=fake_tools)
    a.ask("hi")
    assert "output_config" not in client.requests[0] and a.effort == "n/a"


def test_unknown_model_is_refused():
    with pytest.raises(ValueError):
        Agent(model="gpt-oops", client=FakeClient([]))


def test_trace_json_is_serialisable():
    client = FakeClient([resp([tool_use("describe_data", {}, 1)]), resp([text("x")], stop="end_turn")])
    t = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools).ask("q")
    json.dumps(t.to_json(), default=str)


def test_events_stream_tool_progress_and_a_broken_listener_is_harmless():
    events: list[dict[str, Any]] = []
    client = FakeClient([
        resp([tool_use("describe_data", {}, 1), tool_use("run_sql", {"query": "SELECT 1"}, 2)]),
        resp([text("ok")], stop="end_turn"),
    ])  # fmt: skip
    Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, on_event=events.append).ask("q")
    assert [(e["type"], e.get("name")) for e in events] == [
        ("tool_call", "describe_data"), ("tool_result", "describe_data"),
        ("tool_call", "run_sql"), ("tool_result", "run_sql"),
    ]  # fmt: skip
    assert events[1]["output"]["caveats"] == ["caveat from describe_data"]

    def boom(_):
        raise RuntimeError("listener died")

    client = FakeClient([resp([tool_use("describe_data", {}, 1)]), resp([text("still ok")], stop="end_turn")])
    t = Agent(model="claude-sonnet-5", client=client, tool_caller=fake_tools, on_event=boom).ask("q")
    assert t.answer == "still ok"
