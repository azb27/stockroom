# ADR 0006: MCP server built from the tool registry, loopback-only HTTP, short-lived app DB connections

**Status:** accepted · **Phase:** P6 (refines ADR 0002, which named FastMCP)

## Context
The MCP server is the second front door to the six tools. The P5 accuracy numbers only carry over to Claude Desktop and Claude Code if this door behaves exactly like the first one: same schemas, same guards, same caveats. It is also the first long-lived process that writes to `app.duckdb`. DuckDB lets only one process hold a writable file, so a running server would have locked the buyer out of `stockroom.approvals`. We reproduced this (`test_a_second_process_can_approve_while_a_tool_process_is_alive` fails on the old code).

## Decision
- **Use the MCP SDK's low-level `Server`, not FastMCP.**
  - Tool names, descriptions and JSON schemas come straight from `stockroom.tools.TOOLS`. Every call goes through `stockroom.tools.call`.
  - The SDK checks arguments against those schemas. `additionalProperties: false` therefore blocks the eval-only `schema="raw"` mode at the protocol layer as well as in `call`.
- **Tool annotations:**
  - Five tools are `readOnlyHint`.
  - `draft_reorder` is a non-destructive write.
  - All six are `openWorldHint: false`.
  - The Claude Code skill pre-approves only the read-only five, so a human confirms before drafts are written.
- **Server `instructions`** reuse the agent's rules (`prompts.RULES`), so any client gets "missing day = UNKNOWN" and "drafts only" without the skill.
- **Result format:** the envelope serialised as JSON with the caveats first. A client that truncates long output drops rows, not warnings.
- **Transports:**
  - stdio is the default.
  - Streamable HTTP binds to loopback only; any other host is refused. It runs stateless with DNS-rebinding protection (Host/Origin allow-list).
  - There is no auth. Remote access belongs to P7's API.
- **Audit:** every call is appended to `runs/mcp/calls.jsonl` with the client name, arguments, duration and any error. Nothing is written to stdout, which carries the stdio protocol.
- **`app.duckdb` access:** opened per operation (`appdb.app_session`) and retried for up to 10 s if another process holds it. The drafts from one `draft_reorder` call are written in a single transaction.

## Consequences
- Parity tests compare MCP results with `call()` field by field, apart from timings. A live `claude -p` run over MCP alone scored 8/8 on eval questions (`docs/results/claude_code_session_p6.md`).
- Opening the file per operation costs about 13 ms (measured on the dev container). That is irrelevant at human-paced tool calls, and P7's API inherits working concurrency.
- In Claude Code the MCP guards protect only the MCP path, because the user's own Bash and Read tools can still open the DuckDB files. The skill forbids this, and the scored run disables built-in tools (`--tools Skill`) to prove answers came through MCP.

## Alternatives considered
- **FastMCP with decorated functions:** less code, and it's what the spec named. But it derives schemas from Python signatures, so enums, bounds and `additionalProperties` would either be duplicated in type hints or lost. Two sources of truth drift, and the eval measured one of them.
- **FastMCP plus overriding its internal handlers:** this works today but depends on private attributes (`_mcp_server`, `_tool_manager`).
- **Auth on the HTTP transport now:** it's needed for any non-local use. But the demo only needs loopback, P7 owns the public surface, and a half-built token scheme is worse than a hard refusal to bind publicly.
- **Keep one app DB connection and serialise through the server:** this would push every writer (CLI, UI) through a network service. That is heavier than per-operation connections for a single-user tool.
