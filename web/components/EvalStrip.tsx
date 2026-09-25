import type { Meta } from "@/lib/api";

const pct = (x: number) => `${Math.round(x)}%`;

export default function EvalStrip({ meta }: { meta: Meta }) {
  const c = meta.eval?.configs;
  const sonnet = c?.sonnet;
  const raw = c?.sonnet_raw;
  const used = Math.min(1, meta.spent_today_usd / meta.daily_budget_usd);
  return (
    <section className="stats" aria-label="Evaluation results">
      {sonnet && (
        <div className="tile">
          <div className="label">Correct on {meta.eval?.n_questions} ground-truth questions</div>
          <div className="value">{pct(sonnet.overall.acc)}</div>
          <div className="sub">
            {sonnet.label}, 95% CI {pct(sonnet.overall.lo)}–{pct(sonnet.overall.hi)}
          </div>
        </div>
      )}
      {raw && (
        <div className="tile">
          <div className="label">Same model without the cleaning layer</div>
          <div className="value">{pct(raw.overall.acc)}</div>
          <div className="sub">
            95% CI {pct(raw.overall.lo)}–{pct(raw.overall.hi)}; {pct(raw["T1+T2"].acc)} on lookups and totals
          </div>
        </div>
      )}
      {sonnet && (
        <div className="tile">
          <div className="label">Cost per question</div>
          <div className="value">${sonnet.cost_per_q_usd.toFixed(3)}</div>
          <div className="sub">median {sonnet.p50_latency_s.toFixed(1)} s, {sonnet.label}</div>
        </div>
      )}
      <div className="tile">
        <div className="label">Demo budget today</div>
        <div className="value">
          ${meta.spent_today_usd.toFixed(2)} <span className="sub">of ${meta.daily_budget_usd.toFixed(2)}</span>
        </div>
        <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(used * 100)} aria-label="Share of today's demo budget used">
          <span style={{ width: `${used * 100}%` }} />
        </div>
        <div className="sub">
          {meta.questions_per_hour} questions/hour per visitor; resets 00:00 UTC
        </div>
      </div>
    </section>
  );
}
