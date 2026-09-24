"""Talk to the agent from a terminal.

    python -m stockroom.agent                      # interactive chat
    python -m stockroom.agent --ask "question"     # one question, then exit
    python -m stockroom.agent --ask "..." --steps  # also print every tool call
    options: --model claude-haiku-4-5-20251001  --effort low|medium|high|xhigh|max

Needs ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import json
import sys

from stockroom.agent.loop import Agent, ConversationClosed
from stockroom.agent.tracing import TurnTrace


def render(t: TurnTrace, show_steps: bool) -> str:
    lines = []
    if show_steps:
        for i, s in enumerate(t.steps, 1):
            arg = json.dumps(s.input)
            lines.append(f"  [{i}] {s.tool}({arg[:160]}{'...' if len(arg) > 160 else ''})"
                         f"{'  -> ERROR: ' + s.output['error'][:120] if s.is_error else ''}")  # fmt: skip
        lines.append("")
    lines.append(t.answer or "(no answer)")
    u = t.usage
    lines.append(
        f"\n-- {t.tool_calls} tool calls · {u.get('input', 0) + u.get('cache_read', 0) + u.get('cache_write', 0):,} in "
        f"({u.get('cache_read', 0):,} cached) / {u.get('output', 0):,} out · ${t.cost_usd:.4f} · {t.latency_s:.1f}s"
        + (f" · limits: {', '.join(t.limits_hit)}" if t.limits_hit else "")
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m stockroom.agent")
    ap.add_argument("--ask")
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--steps", action="store_true", help="print each tool call")
    a = ap.parse_args()
    agent = Agent(model=a.model, effort=a.effort)
    if a.ask:
        print(render(agent.ask(a.ask), a.steps))
        return
    print(f"Stockroom agent ({agent.model}, effort {agent.effort}). Ctrl-D to quit.")
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        try:
            print("\n" + render(agent.ask(q), a.steps))
        except ConversationClosed as e:
            print(f"\n{e}")
            return
        except Exception as e:
            print(f"\nerror: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
