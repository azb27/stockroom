"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { type Answer, type Meta, streamChat } from "@/lib/api";
import Trace, { type Step } from "./Trace";

type Turn = { question: string; steps: Step[]; answer?: Answer; error?: string; running: boolean; limits: string[] };

function summary(t: Turn): string {
  const n = t.steps.length;
  const calls = `${n} tool call${n === 1 ? "" : "s"}`;
  if (t.running) return n ? `Working… ${calls}` : "Thinking…";
  if (!t.answer) return calls;
  return `${calls} · ${t.answer.latency_s.toFixed(1)} s · $${t.answer.cost_usd.toFixed(3)}`;
}

export default function Chat({ meta, onDrafted, onSpent }: { meta: Meta; onDrafted: () => void; onSpent: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const conversation = useRef<string | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [turns]);

  const update = (fn: (t: Turn) => Turn) => setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

  async function ask(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setInput("");
    setTurns((ts) => [...ts, { question: q, steps: [], running: true, limits: [] }]);
    let drafted = false;
    try {
      for await (const ev of streamChat(q, conversation.current)) {
        switch (ev.event) {
          case "start":
            conversation.current = ev.data.conversation_id;
            break;
          case "tool_call":
            update((t) => ({ ...t, steps: [...t.steps, { call: ev.data }] }));
            break;
          case "tool_result":
            if (ev.data.name === "draft_reorder" && !ev.data.is_error) drafted = true;
            update((t) => ({
              ...t,
              steps: t.steps.map((s) => (s.call.id === ev.data.id ? { ...s, result: ev.data } : s)),
            }));
            break;
          case "limit":
            update((t) => ({ ...t, limits: [...t.limits, ...ev.data.reasons] }));
            break;
          case "answer":
            update((t) => ({ ...t, answer: ev.data }));
            break;
          case "error":
            update((t) => ({ ...t, error: ev.data.message }));
            break;
        }
      }
    } catch (e) {
      update((t) => ({ ...t, error: e instanceof Error ? e.message : String(e) }));
    } finally {
      update((t) => ({ ...t, running: false }));
      setBusy(false);
      onSpent();
      if (drafted) onDrafted();
    }
  }

  function reset() {
    conversation.current = null;
    setTurns([]);
  }

  const disabled = !meta.chat_enabled;
  return (
    <section className="panel chat" aria-label="Chat with the agent">
      <div className="panel-head">
        <h2>Ask the ops agent</h2>
        <button className="btn small" onClick={reset} disabled={busy || turns.length === 0}>
          New chat
        </button>
      </div>
      <div className="messages" aria-live="polite">
        {disabled && (
          <div className="notice">
            {meta.chat_disabled_reason}{" "}
            <a href={`${meta.repo_url}/blob/main/docs/results/eval.md`} target="_blank" rel="noreferrer">
              Full eval report
            </a>{" "}
            ·{" "}
            <a href={`${meta.repo_url}/blob/main/docs/results/claude_code_session_p6.md`} target="_blank" rel="noreferrer">
              recorded session transcripts
            </a>
          </div>
        )}
        {turns.length === 0 && !disabled && (
          <>
            <p className="empty">
              The data is a distributor&apos;s ERP and point-of-sale history for four stores. It has a half-finished
              SKU migration, a file loaded twice, prices keyed in cents and a two-day outage. Today is {meta.as_of}. Try:
            </p>
            <div className="examples">
              {meta.examples.map((e) => (
                <button key={e} onClick={() => ask(e)}>
                  {e}
                </button>
              ))}
            </div>
          </>
        )}
        {turns.map((t, i) => (
          <div key={i} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="q">{t.question}</div>
            <div className="a">
              <Trace steps={t.steps} running={t.running} summary={summary(t)} />
              {t.answer && (
                <div className="answer">
                  <ReactMarkdown>{t.answer.text}</ReactMarkdown>
                  {t.answer.limits_hit.length > 0 && (
                    <div className="meta-line">Stopped early: {t.answer.limits_hit.join(", ")}</div>
                  )}
                </div>
              )}
              {t.error && <div className="notice error">{t.error}</div>}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask(input);
            }
          }}
          placeholder={disabled ? "Chat is offline" : "Ask about sales, stock-outs, forecasts or reorders…"}
          maxLength={1000}
          disabled={disabled}
          aria-label="Question"
        />
        <button className="btn primary" type="submit" disabled={disabled || busy || !input.trim()}>
          {busy ? "Working…" : "Ask"}
        </button>
      </form>
    </section>
  );
}
