# Changelog

## [0.1.0] — 2026-09-18

First public release.

### Added
- `bulk_read` — delegate reading files to a worker model; only the answer
  enters the agent's context.
- `code_write` — generate code from a spec and a reference file, optionally
  written straight to disk so the generated code never enters context.
- `status` — print the live configuration and probe the worker.
- PreToolUse hook that blocks `Read` on files over a line or byte threshold
  and redirects to `bulk_read`. Opt-in, reversible.
- `token-save-mcp init` — one-command setup, no hand-edited JSON.
- `token-save-mcp doctor` — diagnoses config, dependencies, registration and
  makes a live worker call.
- Provider presets: Ollama Cloud, OpenRouter, DeepSeek, Groq, local runtimes.
  Any OpenAI-compatible endpoint via `TOKENSAVE_BASE_URL`.
- 89 offline tests plus 16 hook routing tests; the transport is stubbed, so the
  suite costs nothing to run.

### Notes
- Works with both `mcp` 1.x (`FastMCP`) and 2.x (`MCPServer`).
- A SOCKS proxy without `httpx[socks]` now produces an actionable message
  instead of a stack trace from inside httpx.
