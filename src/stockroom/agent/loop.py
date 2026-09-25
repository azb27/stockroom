"""The agent loop: plain Anthropic Messages API, ~one screen of logic (ADR 0002).

    agent = Agent()
    turn = agent.ask("What were CA_3's top 5 SKUs by revenue last week?")
    print(turn.answer, turn.cost_usd)

Guarantees, each covered by tests/test_agent.py with a scripted fake model:
  * at most `max_tool_calls` tool calls per turn, then the model must answer with what it has
  * a per-conversation cost cap: past it, the model gets one last no-tools call, then the
    conversation refuses new questions
  * a wall-clock limit per turn
  * tools are only reachable through stockroom.tools.call (so the SQL guard, argument checks and
    PENDING-only drafts always apply), and tool errors go back to the model as recoverable errors
  * every turn is traced (app.duckdb + runs/*.jsonl)
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import anthropic

from stockroom import config
from stockroom.agent import tracing
from stockroom.agent.pricing import EFFORT_MODELS, Usage, check_model, cost_usd
from stockroom.agent.prompts import system_prompt
from stockroom.tools import TOOLS, call

MAX_RESULT_CHARS = 15_000


class ConversationClosed(RuntimeError):
    """Raised when a conversation has used its budget; start a new Agent."""


def _tool_result_text(out: dict[str, Any]) -> str:
    # caveats first, so they are never the part that gets truncated
    ordered = {k: out[k] for k in ("error", "caveats", "data", "provenance") if k in out}
    s = json.dumps(ordered, default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + ' ... [result truncated; aggregate further or add LIMIT]"'
    return s


class Agent:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_tool_calls: int = 12,
        max_cost_usd: float = 0.50,
        max_seconds: float = 60.0,
        max_tokens: int = 8000,
        client: Any = None,
        tool_caller: Callable[[str, dict[str, Any]], dict[str, Any]] = call,
        record: bool = True,
        tool_names: list[str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.model = model or config.MODEL
        self.effort = (effort or config.EFFORT) if (model or config.MODEL) in EFFORT_MODELS else "n/a"
        check_model(self.model)
        self.max_tool_calls, self.max_cost_usd, self.max_seconds = max_tool_calls, max_cost_usd, max_seconds
        self.max_tokens = max_tokens
        if client is None:
            client = anthropic.Anthropic(max_retries=3)
        self.client = client
        self.tool_caller = tool_caller
        self.record = record
        self.tools = [t.anthropic_spec() for n, t in TOOLS.items() if tool_names is None or n in tool_names]
        self.messages: list[dict[str, Any]] = []
        self.usage = Usage()
        self.conversation_id = uuid.uuid4().hex[:12]
        self.turn = 0
        self.on_event = on_event  # live progress for UIs: tool_call / tool_result / limit events

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a broken listener must never break the turn
                pass

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.usage)

    def _create(self, final: bool) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt(),
            tools=self.tools,
            messages=self.messages,
            cache_control={"type": "ephemeral"},  # automatic prefix caching across loop iterations
        )
        if self.model in EFFORT_MODELS:
            kwargs["output_config"] = {"effort": self.effort}
        if final:
            kwargs["tool_choice"] = {"type": "none"}
        return self.client.messages.create(**kwargs)

    def ask(self, question: str) -> tracing.TurnTrace:
        if self.cost_usd >= self.max_cost_usd:
            raise ConversationClosed(
                f"conversation budget of ${self.max_cost_usd:.2f} used; start a new conversation"
            )
        self.turn += 1
        t0 = time.monotonic()
        trace = tracing.TurnTrace(
            conversation_id=self.conversation_id, turn=self.turn, started_at=dt.datetime.now(),
            model=self.model, effort=self.effort, question=question,
        )  # fmt: skip
        turn_usage = Usage()
        self.messages.append({"role": "user", "content": question})
        final = False

        while True:
            resp = self._create(final)
            turn_usage.add(resp.usage)
            self.usage.add(resp.usage)
            self.messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                trace.answer = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
                trace.stop_reason = resp.stop_reason
                if resp.stop_reason == "max_tokens":
                    trace.limits_hit.append("max_tokens")
                break

            results: list[dict[str, Any]] = []
            for b in tool_uses:
                if len(trace.steps) >= self.max_tool_calls:
                    out = {
                        "error": "tool-call budget for this question is used up; answer with what you have"
                    }
                    ms = 0.0
                else:
                    self._emit(
                        {"type": "tool_call", "id": b.id, "name": b.name, "input": dict(b.input or {})}
                    )
                    s0 = time.perf_counter()
                    out = self.tool_caller(b.name, dict(b.input or {}))
                    ms = (time.perf_counter() - s0) * 1000
                    trace.steps.append(
                        tracing.Step(b.name, dict(b.input or {}), out, round(ms, 1), "error" in out)
                    )
                    self._emit(
                        {"type": "tool_result", "id": b.id, "name": b.name, "ms": round(ms, 1), "output": out}
                    )
                results.append(
                    {"type": "tool_result", "tool_use_id": b.id, "content": _tool_result_text(out),
                     **({"is_error": True} if "error" in out else {})}
                )  # fmt: skip

            reasons = []
            if len(trace.steps) >= self.max_tool_calls:
                reasons.append(f"tool-call limit ({self.max_tool_calls})")
            if self.cost_usd >= self.max_cost_usd:
                reasons.append(f"cost limit (${self.max_cost_usd:.2f})")
            if time.monotonic() - t0 >= self.max_seconds:
                reasons.append(f"time limit ({self.max_seconds:.0f}s)")
            if reasons:
                final = True
                trace.limits_hit += reasons
                self._emit({"type": "limit", "reasons": reasons})
                results.append(
                    {"type": "text", "text": f"[System: {', '.join(reasons)} reached. No more tools. Answer now "
                     "with what you have and say plainly what you could not check.]"}
                )  # fmt: skip
            self.messages.append({"role": "user", "content": results})

        trace.usage = turn_usage.as_dict()
        trace.cost_usd = round(cost_usd(self.model, turn_usage), 6)
        trace.conversation_cost_usd = round(self.cost_usd, 6)
        trace.latency_s = round(time.monotonic() - t0, 2)
        if self.record:
            tracing.record(trace)
        return trace
