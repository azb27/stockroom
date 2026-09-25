// Thin client for the Stockroom API. Every call carries the visitor's session id.

export type EvalCI = { acc: number; lo: number; hi: number };
export type EvalConfig = {
  label: string;
  overall: EvalCI;
  "T1+T2": EvalCI;
  cost_per_q_usd: number;
  p50_latency_s: number;
};
export type Meta = {
  as_of: string;
  model: string;
  chat_enabled: boolean;
  chat_disabled_reason: string | null;
  daily_budget_usd: number;
  spent_today_usd: number;
  questions_per_hour: number;
  conversation_budget_usd: number;
  examples: string[];
  eval: { n_questions: number; configs: Record<string, EvalConfig> } | null;
  repo_url: string;
};

export type ToolCall = { id: string; name: string; input: Record<string, unknown> };
export type ToolResult = {
  id: string;
  name: string;
  ms: number;
  is_error: boolean;
  error: string | null;
  caveats: string[];
  sql: string | null;
  table?: { columns: string[]; rows: unknown[][]; row_count: number; truncated: boolean };
  preview?: string;
};
export type Answer = {
  text: string;
  cost_usd: number;
  conversation_cost_usd: number;
  latency_s: number;
  tool_calls: number;
  limits_hit: string[];
};
export type Draft = {
  draft_id: string;
  created_at: string;
  store: string;
  supplier_id: string;
  status: "PENDING_APPROVAL" | "APPROVED" | "REJECTED";
  n_lines: number;
  total_cases: number;
  total_units: number;
  est_cost: number;
  below_supplier_minimum: boolean;
  decided_by: string | null;
  decision_note: string | null;
};
export type DraftLine = {
  sku: string;
  on_hand: number;
  on_order: number;
  lead_time_days: number;
  forecast_units: number;
  safety_units: number;
  case_pack: number;
  case_pack_imputed: boolean;
  order_cases: number;
  order_units: number;
  unit_cost: number;
  est_cost: number;
};

export type ChatEvent =
  | { event: "start"; data: { conversation_id: string } }
  | { event: "tool_call"; data: ToolCall }
  | { event: "tool_result"; data: ToolResult }
  | { event: "limit"; data: { reasons: string[] } }
  | { event: "answer"; data: Answer }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: Record<string, never> };

function newSessionId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

let cached: string | null = null;
export function sessionId(): string {
  if (cached) return cached;
  try {
    // Survives a reload in this tab; blocked storage (private mode, some iframes) just means a new session.
    const stored = window.sessionStorage.getItem("stockroom-session");
    if (stored && /^[0-9a-f]{32}$/.test(stored)) return (cached = stored);
    cached = newSessionId();
    window.sessionStorage.setItem("stockroom-session", cached);
  } catch {
    cached = cached ?? newSessionId();
  }
  return cached;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Session-Id": sessionId(), ...(init.headers ?? {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
  return body as T;
}

export const getMeta = () => request<Meta>("/api/meta");
export const getDrafts = () => request<Draft[]>("/api/drafts");
export const getDraftLines = (id: string) =>
  request<{ lines: DraftLine[] }>(`/api/drafts/${encodeURIComponent(id)}`).then((r) => r.lines);
export const decideDraft = (id: string, decision: "approve" | "reject", by: string, note: string) =>
  request<{ status: string }>(`/api/drafts/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, by, note: note || null }),
  });

/** POST a question and yield server-sent events as they arrive. */
export async function* streamChat(message: string, conversationId: string | null): AsyncGenerator<ChatEvent> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Session-Id": sessionId() },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    yield { event: "error", data: { message: body.error ?? `HTTP ${res.status}` } };
    return;
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (event) yield { event, data: JSON.parse(data || "{}") } as ChatEvent;
    }
  }
}
