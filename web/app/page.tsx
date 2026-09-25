"use client";

import { useCallback, useEffect, useState } from "react";
import Chat from "@/components/Chat";
import Drafts from "@/components/Drafts";
import EvalStrip from "@/components/EvalStrip";
import { type Draft, type Meta, getDrafts, getMeta } from "@/lib/api";

export default function Home() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refreshMeta = useCallback(() => {
    getMeta().then(setMeta, (e) => setErr(String(e)));
  }, []);
  const refreshDrafts = useCallback(() => {
    getDrafts().then(setDrafts, () => undefined);
  }, []);

  useEffect(() => {
    refreshMeta();
    refreshDrafts();
  }, [refreshMeta, refreshDrafts]);

  const repo = meta?.repo_url ?? "https://github.com/azb27/stockroom";
  return (
    <main className="wrap">
      <header className="top">
        <div className="brand">
          <h1>Stockroom</h1>
          <p>
            An ops agent for a distributor&apos;s messy data. It answers questions, flags stock-outs, forecasts demand and
            drafts purchase orders that a human approves. Every tool call is shown.
          </p>
        </div>
        <nav className="links">
          <a href={repo} target="_blank" rel="noreferrer">
            GitHub
          </a>
          <a href={`${repo}/blob/main/docs/results/eval.md`} target="_blank" rel="noreferrer">
            Eval report
          </a>
          <a href={`${repo}/blob/main/docs/results/eval_failure_analysis.md`} target="_blank" rel="noreferrer">
            Where it fails
          </a>
        </nav>
      </header>

      {err && <div className="notice error">Could not reach the API: {err}</div>}
      {meta && <EvalStrip meta={meta} />}

      {meta && (
        <div className="grid">
          <Chat meta={meta} onDrafted={refreshDrafts} onSpent={refreshMeta} />
          <Drafts drafts={drafts} onChanged={refreshDrafts} />
        </div>
      )}

      <p className="foot">
        Data: the M5 forecasting dataset (Walmart via the University of Nicosia), four California stores, with synthetic
        ERP attributes and deliberately injected data problems. Model: {meta?.model ?? "…"}. Drafts and chats are kept per
        browser tab and cleared when the demo restarts.
      </p>
    </main>
  );
}
