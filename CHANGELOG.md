# Changelog

## [0.4.0] — 2026-09-18

### Added
- **`run_command`** — run a command and get its verdict instead of its output.
  A failing test suite prints hundreds of lines of setup noise around the four
  that matter; those hundreds go to the worker and the verdict comes back.
  Measured 93% on a real 168-line failure.

  The full output is written to `~/.token-save/logs/` and the path is returned,
  so a summary that missed something costs you nothing — read the part you
  need. The last 50 runs are kept.

  Short output (under ~400 tokens) is returned verbatim rather than sent to a
  worker: delegating it would cost more than it saves. If the worker call
  fails, the last 25 lines come back rather than nothing.

Command output is the second-largest context leak after file reads, and unlike
file reads nothing else addresses it well — truncating by length throws away
the stack trace, which is the one part that mattered.

## [0.3.0] — 2026-09-18

### Added
- **`stats` now flags files you delegate over and over**, and distinguishes the
  two cases: the same question repeated is waste — that answer was already paid
  for — while different questions about one file are legitimate, though batching
  them into one call is cheaper. Each finding carries the worker tokens spent on
  that file and advice you can act on.

### Changed
- The ledger now records file paths and a **hash** of the question. A hash
  answers "was this asked before?" without writing your prompts to disk.
  Entries written by earlier versions have neither; `stats` says so plainly
  rather than reporting nothing found.

Prompted by reading [codeburn](https://github.com/getagentseal/codeburn), which
detects re-read files from session logs. It cannot see *what was asked*, so it
cannot tell a wasteful repeat from a legitimate follow-up. This ledger can.

## [0.2.2] — 2026-09-18

### Changed
- **Added "Choosing the worker model".** The only mentions of changing the model
  were a line inside a collapsed block and a cell in the configuration table at
  the very end — so the obvious question ("can I use my own model?") had no
  visible answer. Now a section right after Install: there is no fixed roster,
  how to switch provider or model, how to override for a single call, and which
  tier to pick if you have no opinion.
- Documented that `--model` and `TOKENSAVE_MODEL` are equivalent and that the
  flag wins, and that `--provider` stops mattering once `TOKENSAVE_BASE_URL`
  is set.
- Removed the duplicate "any OpenAI-compatible endpoint" block so there is one
  place describing this, not two.

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
