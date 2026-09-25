"use client";

import { useState } from "react";
import { type Draft, type DraftLine, decideDraft, getDraftLines } from "@/lib/api";

const money = (x: number) => x.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function Badge({ status }: { status: Draft["status"] }) {
  const map = {
    PENDING_APPROVAL: ["pending", "Pending approval"],
    APPROVED: ["approved", "Approved"],
    REJECTED: ["rejected", "Rejected"],
  } as const;
  const [cls, label] = map[status];
  return (
    <span className={`badge ${cls}`}>
      <span className="dot" aria-hidden />
      {label}
    </span>
  );
}

const SHOWN = 12;

function Lines({ lines }: { lines: DraftLine[] }) {
  const top = lines.slice(0, SHOWN); // the API sorts lines by cost, largest first
  return (
    <div className="mini lines">
      <table>
        <thead>
          <tr>
            <th>SKU</th>
            <th title="Units on hand today">On hand</th>
            <th title="Forecast demand over lead time + review period">Fcst</th>
            <th>Cases</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {top.map((l) => (
            <tr key={l.sku}>
              <td>
                {l.sku}
                {l.case_pack_imputed ? " *" : ""}
              </td>
              <td>{l.on_hand}</td>
              <td>{l.forecast_units.toFixed(0)}</td>
              <td>{l.order_cases}</td>
              <td>{money(l.est_cost)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="ms">
        {lines.length > SHOWN ? `Top ${SHOWN} of ${lines.length} lines by cost. ` : ""}
        {lines.some((l) => l.case_pack_imputed) ? "* case pack imputed; confirm with the supplier." : ""}
      </div>
    </div>
  );
}

function DraftCard({ d, onChanged }: { d: Draft; onChanged: () => void }) {
  const [lines, setLines] = useState<DraftLine[] | null>(null);
  const [by, setBy] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function toggle() {
    if (lines) return setLines(null);
    try {
      setLines(await getDraftLines(d.draft_id));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setErr(null);
    try {
      await decideDraft(d.draft_id, decision, by, note);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="draft">
      <div className="draft-head">
        <span className="draft-id">{d.draft_id}</span>
        <Badge status={d.status} />
      </div>
      <div className="draft-facts">
        {d.store} · supplier {d.supplier_id} · {d.n_lines} lines · {d.total_cases} cases · {money(d.est_cost)}
      </div>
      {d.below_supplier_minimum && <div className="caveat">Below the supplier&apos;s minimum order; not inflated.</div>}
      {d.decided_by && (
        <div className="ms">
          {d.status === "APPROVED" ? "Approved" : "Rejected"} by {d.decided_by}
          {d.decision_note ? `: “${d.decision_note}”` : ""}
        </div>
      )}
      <button className="btn small" onClick={toggle} style={{ marginTop: 6 }}>
        {lines ? "Hide lines" : "Show lines"}
      </button>
      {lines && <Lines lines={lines} />}
      {d.status === "PENDING_APPROVAL" && (
        <div className="decide">
          <input placeholder="Your name (required)" value={by} onChange={(e) => setBy(e.target.value)} aria-label="Your name" />
          <input placeholder="Note (required to reject)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" />
          <button className="btn small primary" disabled={busy || !by.trim()} onClick={() => decide("approve")}>
            Approve
          </button>
          <button className="btn small" disabled={busy || !by.trim() || !note.trim()} onClick={() => decide("reject")}>
            Reject
          </button>
        </div>
      )}
      {err && <div className="err">{err}</div>}
    </div>
  );
}

export default function Drafts({ drafts, onChanged }: { drafts: Draft[]; onChanged: () => void }) {
  return (
    <section className="panel" aria-label="Purchase order approval queue">
      <div className="panel-head">
        <h2>PO approval queue</h2>
        <span className="ms">{drafts.filter((d) => d.status === "PENDING_APPROVAL").length} pending</span>
      </div>
      <div className="panel-body drafts">
        <p className="empty" style={{ margin: 0 }}>
          The agent can only <b>draft</b> orders. Approving is a human action on this panel, and the agent has no tool
          that reaches it. Nothing is sent to a supplier in this demo.
        </p>
        {drafts.length === 0 ? (
          <p className="empty" style={{ margin: 0 }}>
            No drafts yet. Ask the agent to “draft a reorder for CA_2 HOBBIES_1”.
          </p>
        ) : (
          drafts.map((d) => <DraftCard key={d.draft_id} d={d} onChanged={onChanged} />)
        )}
      </div>
    </section>
  );
}
