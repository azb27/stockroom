# ADR 0002: One tool library, exposed via an MCP server and a hand-written Client SDK loop

**Status:** accepted · **Phase:** P2/P4/P6

## Context
Three plausible ways to run the agent:
1. **Anthropic Client SDK** (Messages API) with our own tool loop, or the SDK's beta tool runner.
2. **Claude Agent SDK**, which embeds Claude Code's agent loop and runs the Claude Code binary, with built-in file/shell tools, hooks, sessions and permissions.
3. **Tools only as an MCP server,** with Claude Desktop or Claude Code as the agent.

## Decision
- Tools are plain, typed Python functions in `stockroom.tools`. They are the single source of behaviour.
- **Web agent:** a Client SDK loop we write ourselves (~150 lines) that calls tool functions in-process. We need exact control over:
  - the approval gate
  - per-conversation cost caps
  - tracing into our own tables
  - deterministic eval runs
- **MCP server:** a FastMCP wrapper over the same functions (stdio and streamable HTTP), so the tools plug into Claude Desktop, Claude Code or any MCP client. Ships with a Claude Code skill.
- **Agent SDK:** deferred to P9 for an autonomous overnight replenishment job, where sessions, hooks (a `PreToolUse` approval policy) and subagents are worth the extra runtime.

## Consequences
- Two thin adapters over one library. Tests target the library.
- The web agent has no file or shell access to lock down, because it never had any.
- In interviews, we can explain both halves: the "why MCP" (portability, customer-owned clients) and the "why not the Agent SDK here" (runtime weight, attack surface vs. what a read-mostly ops agent needs).

## Alternatives considered
- **Agent SDK for the web agent:** most keyword-friendly, but it runs the Claude Code binary per session and brings tools we'd have to disable. It's overkill for 6 domain tools.
- **LangGraph:** appears in many job descriptions, but adds a framework between us and the API with no capability we need. MCP tools port to it if a role demands it.
- **Web agent calling our MCP server over HTTP:** purer, but it adds a network hop and a failure mode for no user-visible gain.
