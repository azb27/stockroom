"""Web API for the demo: chat over server-sent events, plus the human PO approval queue.

    stockroom-web                                   # http://127.0.0.1:7860

Two kinds of routes, and they never mix:
- The **agent** runs only through `POST /api/chat`. It reaches data only through the tool registry, the
  same one the P5 eval measured.
- **Humans** decide drafts through `POST /api/drafts/{id}/decision`, which calls `stockroom.approvals`.
  The agent has no tool, route or code path that reaches it.

Each visitor is a session (an `X-Session-Id` header the page generates). Sessions see only their own
conversations and drafts. Cookies aren't used because the page runs inside Hugging Face's iframe, where
browsers block third-party cookies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stockroom import appdb, approvals, config
from stockroom.agent.loop import Agent, ConversationClosed
from stockroom.api.guards import DailyBudget, RateLimiter
from stockroom.tools.base import as_of

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
EXAMPLES = [
    "What were CA_4's total unit sales on 2016-03-14?",
    "Which 5 FOODS_3 SKUs at CA_3 are most at risk of running out within their supplier lead time?",
    "Why did revenue at CA_1 change between the 4 weeks ending 2016-04-22 and the 4 weeks ending 2016-05-20?",
    "Draft a reorder for CA_2 HOBBIES_1.",
    "How many units of foods-3-090 did CA_1 sell in October 2015?",
]
PREVIEW_ROWS = 20


@dataclass
class Settings:
    daily_budget_usd: float = float(os.environ.get("STOCKROOM_DAILY_BUDGET_USD", "0.50"))
    conversation_budget_usd: float = float(os.environ.get("STOCKROOM_CONV_BUDGET_USD", "0.10"))
    questions_per_hour: int = int(os.environ.get("STOCKROOM_QUESTIONS_PER_HOUR", "10"))
    max_concurrent: int = int(os.environ.get("STOCKROOM_MAX_CONCURRENT", "2"))
    max_conversations: int = 500
    max_message_chars: int = 1000
    trust_proxy: bool = os.environ.get("STOCKROOM_TRUST_PROXY", "0") == "1"  # set on HF Spaces
    model: str = config.MODEL
    static_dir: Path | None = field(default_factory=lambda: _default_static())
    summary_path: Path = config.ROOT / "docs" / "results" / "eval_summary.json"
    repo_url: str = os.environ.get("STOCKROOM_REPO_URL", "https://github.com/azb27/stockroom")


def _default_static() -> Path | None:
    p = Path(os.environ.get("STOCKROOM_WEB_DIR", config.ROOT / "web" / "out"))
    return p if p.exists() else None


@dataclass
class Conversation:
    agent: Agent
    session: str
    last_used: float = field(default_factory=time.monotonic)


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None


class DecisionIn(BaseModel):
    decision: str
    by: str
    note: str | None = None


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _result_view(out: dict[str, Any]) -> dict[str, Any]:
    """What the trace panel needs from a tool result: small, but not misleadingly partial."""
    view: dict[str, Any] = {
        "is_error": "error" in out,
        "error": out.get("error"),
        "caveats": out.get("caveats", []),
        "sql": out.get("provenance", {}).get("sql"),
    }
    data = out.get("data")
    if isinstance(data, dict) and "rows" in data and "columns" in data:
        view["table"] = {
            "columns": data["columns"],
            "rows": data["rows"][:PREVIEW_ROWS],
            "row_count": data.get("row_count", len(data["rows"])),
            "truncated": data.get("truncated", False) or len(data["rows"]) > PREVIEW_ROWS,
        }
    elif data is not None:
        text = json.dumps(data, default=str)
        view["preview"] = text[:1500] + (" …" if len(text) > 1500 else "")
    return view


def create_app(
    settings: Settings | None = None,
    agent_factory: Callable[[Settings], Agent] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    s = settings or Settings()
    make_agent = agent_factory or (
        lambda st: Agent(model=st.model, max_cost_usd=st.conversation_budget_usd, max_seconds=60)
    )
    budget = DailyBudget(s.daily_budget_usd)
    limiter = RateLimiter(s.questions_per_hour, 3600, clock=clock)
    slots = asyncio.Semaphore(s.max_concurrent)
    conversations: OrderedDict[str, Conversation] = OrderedDict()
    summary = json.loads(s.summary_path.read_text()) if s.summary_path.exists() else None

    app = FastAPI(title="Stockroom", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def session_of(x_session_id: str | None) -> str:
        if not x_session_id or not SESSION_RE.match(x_session_id):
            raise HTTPException(400, "missing or malformed X-Session-Id header")
        return x_session_id

    def client_ip(request: Request) -> str:
        if s.trust_proxy and (fwd := request.headers.get("x-forwarded-for")):
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def api_key_present() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY")) or agent_factory is not None

    def chat_status() -> tuple[bool, str | None]:
        if not api_key_present():
            return (
                False,
                "Live chat is offline: no API key is configured. The recorded eval results are below.",
            )
        if not budget.allow():
            return False, (
                f"Today's demo budget (${s.daily_budget_usd:.2f}) is used up; chat reopens at 00:00 UTC. "
                "The recorded eval results are below."
            )
        return True, None

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "warehouse": config.WAREHOUSE_DB.exists(),
            "forecast": config.FORECAST_DB.exists(),
        }

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        enabled, reason = chat_status()
        return {
            "as_of": as_of(),
            "model": s.model,
            "chat_enabled": enabled,
            "chat_disabled_reason": reason,
            "daily_budget_usd": s.daily_budget_usd,
            "spent_today_usd": round(budget.spent_usd, 4),
            "questions_per_hour": s.questions_per_hour,
            "conversation_budget_usd": s.conversation_budget_usd,
            "examples": EXAMPLES,
            "eval": summary,
            "repo_url": s.repo_url,
        }

    def get_conversation(cid: str | None, session: str) -> Conversation:
        if cid:
            conv = conversations.get(cid)
            if conv is None or conv.session != session:
                raise HTTPException(404, "conversation not found; start a new one")
            conversations.move_to_end(cid)
            return conv
        conv = Conversation(make_agent(s), session)
        conversations[conv.agent.conversation_id] = conv
        while len(conversations) > s.max_conversations:
            conversations.popitem(last=False)
        return conv

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request, x_session_id: str | None = Header(default=None)):
        session = session_of(x_session_id)
        if len(body.message) > s.max_message_chars:
            raise HTTPException(413, f"questions are limited to {s.max_message_chars} characters")
        enabled, reason = chat_status()
        if not enabled:
            raise HTTPException(503, reason)
        ok, retry = limiter.hit(client_ip(request))
        if not ok:
            raise HTTPException(
                429,
                f"Limit of {s.questions_per_hour} questions per hour reached; try again in {retry / 60:.0f} min.",
            )
        conv = get_conversation(body.conversation_id, session)
        return StreamingResponse(
            _run_turn(conv, body.message, session),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _run_turn(conv: Conversation, message: str, session: str) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        agent = conv.agent

        def on_event(ev: dict[str, Any]) -> None:
            if ev["type"] == "tool_result":
                ev = {k: v for k, v in ev.items() if k != "output"} | _result_view(ev["output"])
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        yield _sse("start", {"conversation_id": agent.conversation_id})
        try:
            await asyncio.wait_for(slots.acquire(), timeout=30)
        except TimeoutError:
            yield _sse("error", {"message": "The demo is busy right now; please try again in a minute."})
            return
        cost_before = agent.cost_usd

        def finished(_t: asyncio.Future) -> None:
            # Runs when the model thread ends, even if the visitor disconnected mid-answer, so the
            # budget counts every cent and the slot stays taken while the thread is still spending.
            budget.add(agent.cost_usd - cost_before)
            agent.on_event = None
            conv.last_used = time.monotonic()
            slots.release()

        agent.on_event = on_event
        token = appdb.ACTOR.set(f"web:{session}")  # the task copies this context: drafts are tagged
        try:
            task = asyncio.ensure_future(asyncio.to_thread(agent.ask, message))
        finally:
            appdb.ACTOR.reset(token)
        task.add_done_callback(finished)

        while not task.done() or not queue.empty():
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                ev = getter.result()
                yield _sse(ev.pop("type"), ev)
            else:
                getter.cancel()
        try:
            trace = task.result()
        except ConversationClosed as e:
            yield _sse("error", {"message": f"{e}. Click 'New chat' to continue."})
            return
        except Exception as e:  # API outage, etc.: tell the user, don't leak internals
            yield _sse("error", {"message": f"The model call failed ({type(e).__name__}). Please retry."})
            return
        yield _sse(
            "answer",
            {
                "text": trace.answer,
                "cost_usd": trace.cost_usd,
                "conversation_cost_usd": trace.conversation_cost_usd,
                "latency_s": trace.latency_s,
                "tool_calls": trace.tool_calls,
                "limits_hit": trace.limits_hit,
            },
        )
        yield _sse("done", {})

    # ---- human side: the approval queue ------------------------------------------------------------
    def owned(draft_id: str, session: str) -> None:
        with appdb.app_session() as con:
            row = con.execute("SELECT created_by FROM po_drafts WHERE draft_id = ?", [draft_id]).fetchone()
        if row is None or row[0] != f"web:{session}":
            raise HTTPException(404, "draft not found")

    @app.get("/api/drafts")
    def drafts(x_session_id: str | None = Header(default=None)) -> list[dict[str, Any]]:
        session = session_of(x_session_id)
        with appdb.app_session() as con:
            df = con.execute(
                """SELECT draft_id, created_at, store, supplier_id, status, n_lines, total_cases, total_units,
                          est_cost, below_supplier_minimum, decided_by, decided_at, decision_note
                   FROM po_drafts WHERE created_by = ? ORDER BY created_at DESC, draft_id""",
                [f"web:{session}"],
            ).df()
        return json.loads(df.to_json(orient="records", date_format="iso"))

    @app.get("/api/drafts/{draft_id}")
    def draft(draft_id: str, x_session_id: str | None = Header(default=None)) -> dict[str, Any]:
        owned(draft_id, session_of(x_session_id))
        with appdb.app_session() as con:
            lines = con.execute(
                """SELECT sku, on_hand, on_order, lead_time_days, forecast_units, safety_units, case_pack,
                          case_pack_imputed, order_cases, order_units, unit_cost, est_cost
                   FROM po_draft_lines WHERE draft_id = ? ORDER BY est_cost DESC""",
                [draft_id],
            ).df()
        return {"draft_id": draft_id, "lines": json.loads(lines.to_json(orient="records"))}

    @app.post("/api/drafts/{draft_id}/decision")
    def decide(draft_id: str, body: DecisionIn, x_session_id: str | None = Header(default=None)):
        owned(draft_id, session_of(x_session_id))
        try:
            approvals.decide(draft_id, body.decision, by=body.by, note=body.note)
        except approvals.ApprovalError as e:
            raise HTTPException(400, str(e)) from None
        return {"draft_id": draft_id, "status": approvals.DECISIONS[body.decision], "by": body.by.strip()}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # ---- the static Next.js export ------------------------------------------------------------------
    if s.static_dir is not None:
        app.mount("/", StaticFiles(directory=s.static_dir, html=True), name="web")
    else:

        @app.get("/")
        def index_missing():
            return JSONResponse({"error": "web UI not built; run `npm run build` in web/"}, status_code=404)

    app.state.budget, app.state.limiter, app.state.conversations = budget, limiter, conversations
    return app


def main() -> None:
    """`stockroom-web`: serve the API and UI (HF Spaces sets PORT=7860 and HOST=0.0.0.0)."""
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "7860")),
        proxy_headers=True,
        log_level="info",
    )
