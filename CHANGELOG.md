# Changelog

## [0.2.1] — 2026-09-18

Two independent readers were asked to review the README cold. Both found the
same gaps; this release closes them.

### Fixed
- `install-hook` now says plainly when Claude Code is not installed, instead of
  reporting success for a hook that cannot fire. The tools still work in any
  MCP client — only the automatic blocking is Claude Code's.

### Changed
- README opens with the cost of *not* using the tool rather than assuming the
  reader already feels it.
- **Added "Where your code goes"** — the files you name are sent to the provider
  you configured, with a table for open-source / employer / proprietary code and
  the local option for when nothing may leave the machine. This was missing
  entirely, and for a closed codebase it is the first question.
- **Added "What it costs"** in money, not just tokens, including why a read on
  turn 20 of a long session is not paid for once.
- Requirements no longer imply that enforcement works in all five listed
  clients.
- "When not to use this" now admits the real limit: if you would re-read the
  file anyway, you have paid for both.

## [0.2.0] — 2026-09-18

Setup is now genuinely two commands. Three things a new user needed that the
package did not provide are all shipped or removed.

### Fixed
- **`socksio` is now a dependency.** Behind a SOCKS proxy — common on VPNs and
  corporate networks — the first call died with an ImportError from inside
  httpx, and the fix was an undocumented `pip install 'httpx[socks]'`.
- **The hook no longer needs `jq`.** It is Python now and ships in the package.
  Previously it silently passed every read through on any machine without jq,
  so enforcement was off and nobody was told.

### Changed
- README states plainly that this is one package and one command; there is no
  second MCP server to install and no external tool to add.
- `doctor` checks for the hook script instead of jq.
- Documented that browser-login tools (Kimi Code, Copilot) cannot be used as
  the worker — they expose no OpenAI-compatible endpoint.

### Added
- Hook tests for running without jq and for a filename containing a quote.

## [0.1.2] — 2026-09-18

### Fixed
- The OpenAI SDK logs every request at INFO level. On a stdio MCP server that
  noise landed in the user's terminal, mixed into `doctor` output and tool
  results. Silenced to WARNING.

## [0.1.1] — 2026-09-18

### Fixed
- **`init` failed to register the server** whenever it passed environment
  variables: `claude mcp add -e` takes a variadic list and swallowed the server
  name that followed it. Now passed as a single `--env=KEY=value` argument.
  This broke `--provider local` outright — the one path that needs no API key.
- `TOKENSAVE_PROVIDER` was omitted from the registration for the default
  provider, so a later change of default would have silently moved the worker.

### Changed
- **`init` with no arguments now onboards you.** It detects a provider key
  already in your environment and uses it; with several it asks which; with
  none it prints the options, what each costs you, and the exact variable to
  export — instead of failing on a default provider you may never have heard of.
- README states plainly that nothing connects automatically and the worker is
  yours: your key, your provider, your bill.

### Added
- 24 CLI tests covering onboarding and the exact shape of the registration
  command.

## [0.1.0] — 2026-09-18

First public release.

### Added
- `bulk_read` — delegate reading files to a worker model; only the answer
  enters the agent's context.
- `code_write` — generate code from a spec and a reference file, optionally
  written straight to disk so the generated code never enters context.
- `status` — print the live configuration and probe the worker.
- PreToolUse hook that blocks `Read` on files over a line or byte threshold
  and redirects to `bulk_read`. Opt-in, reversible, and available in a softer
  `warn` mode that allows the read but reports what it cost.
- `token-save-mcp init` — one-command setup, no hand-edited JSON.
- `token-save-mcp doctor` — diagnoses config, dependencies, registration and
  makes a live worker call.
- Provider presets: Ollama Cloud, OpenRouter, DeepSeek, Groq, local runtimes.
  Any OpenAI-compatible endpoint via `TOKENSAVE_BASE_URL`.
- `token-save-mcp stats` — a local JSONL ledger of every call, with a
  shareable badge. Never leaves the machine; opt out with `TOKENSAVE_NO_LEDGER`.
- 95 offline tests plus 19 hook routing tests; the transport is stubbed, so the
  suite costs nothing to run.

### Notes
- Works with both `mcp` 1.x (`FastMCP`) and 2.x (`MCPServer`).
- A SOCKS proxy without `httpx[socks]` now produces an actionable message
  instead of a stack trace from inside httpx.
