# ADR 0007: One-container public demo on Hugging Face Spaces, with fail-closed spend guards

**Status:** accepted · **Phase:** P7 (replaces SPEC's "Fly.io/Render backend + Vercel frontend")

## Context
The demo's audience is a recruiter clicking a link weeks after it was posted. It must still load then, cost almost nothing when idle, and not let a stranger run up the owner's API bill. The owner picked Hugging Face Spaces (free, no card) and a **$0.50/day** budget.

## Decision
- **One container, one URL.**
  - The Next.js UI (React + TypeScript) is a static export served by the FastAPI app.
  - Nothing runs on Vercel, so there is no CORS and only one deploy to keep alive.
- **The image carries the data.** It holds the cleaned warehouse and the batch forecasts (58 MB).
  - It does **not** hold `ground_truth.duckdb` or the dirt manifest. The Dockerfile asserts they are absent, and the deploy script refuses to upload them.
  - Files in a public Space are public; both published files derive from M5, which is already hosted publicly on HF.
- **Spend guards, all failing closed:**
  - a daily global budget, with chat off until 00:00 UTC once it's spent
  - 10 questions an hour per IP (from `X-Forwarded-For` behind HF's proxy)
  - $0.10 per conversation
  - 2 concurrent agent runs
  - The cost of a turn is counted when the model thread ends, even if the visitor has disconnected.
  - Backstop outside the code: a separate Anthropic key in its own workspace with a monthly limit. The owner adds it as a Space secret; the deploy script never sees it.
- **Isolation without accounts.**
  - Each tab sends a random `X-Session-Id` header. Conversations and drafts belong to it (drafts via `created_by = web:<session>`).
  - Headers are used instead of cookies because HF renders Spaces in an iframe, where browsers block third-party cookies.
- **Approvals stay human.** Deciding a draft is a separate HTTP route that calls `stockroom.approvals`. The agent's only capabilities are the registry tools.

## Consequences
- **State is lost on restart.** The free tier has no persistent disk, so drafts, traces and the day's spend counter reset when the Space restarts. That's fine for a demo. A restart resetting the budget is a small hole, which the workspace spend limit covers.
- **Cold starts.** Free Spaces sleep after about 48 h without traffic, so the first visitor after a quiet spell waits for a cold start.
- **Budget overshoot.** Worst case is one day's budget plus the in-flight turns (≤ 2 × $0.10).

## Alternatives considered
- **Fly.io, the spec's plan:** wakes in seconds rather than minutes and is closer to production hosting. But it needs a card, and the owner chose free.
- **Next.js on Vercel plus a separate API:** Vercel is a keyword employers look for. But two deploys, CORS and two sets of logs double the ways a months-old demo link can break, for no feature the user sees.
- **Streaming model tokens to the browser:** it feels faster. But the loop's guarantees (caps, the forced final answer) are simpler to show with whole turns, and tool events already give live progress.
- **Accounts or login:** the right way to isolate users. It's too much friction for a portfolio demo whose drafts are throwaway.
