# token-save-mcp

**Your coding agent burns its context reading files. This stops it — and shows you the receipt.**

[![CI](https://github.com/Habartru/token_save_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Habartru/token_save_mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/token-save-mcp.svg)](https://pypi.org/project/token-save-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

An MCP server that sends big files to a cheap worker model and returns only the
answer. The file bytes are paid for once, in the worker's context — not
permanently in your agent's.

```
Reading council_mcp_server.py directly   ≈ 5,788 tokens of context, forever
bulk_read("what's the retry policy?")    ≈   684 tokens — 88% saved
```

Every answer carries the worker's **real token usage from the API response**,
next to the cost of the read it replaced. Not an estimate — a receipt:

```
─────────────────────────────────────────────────────────────
token-save: 1 file(s), 443 lines | direct read ≈5,788 tok →
into Claude ≈684 tok (saved ≈5,104, 88%)
worker: glm-5.3-flash | 5,379 in / 1,165 out | 5.8s
```

---

## Install

```bash
pip install token-save-mcp
export OPENROUTER_API_KEY=...            # or OLLAMA_API_KEY, DEEPSEEK_API_KEY…
token-save-mcp init --provider openrouter --hook
```

That's it — `init` registers the MCP server and installs the hook. No
hand-edited JSON. Drop `--hook` if you want the tools without enforcement.

Verify with `token-save-mcp doctor`, which checks the config and makes one
live call to prove the worker answers:

```
✓ provider: openrouter -> https://openrouter.ai/api/v1
✓ worker model: deepseek/deepseek-chat
✓ MCP server registered and connected
✓ enforcement hook installed
✓ worker replied in 2.0s (21 in / 21 out)
```

<details>
<summary>Providers</summary>

Any OpenAI-compatible endpoint works. Presets:

| `--provider` | Endpoint | Key variable |
|---|---|---|
| `ollama` *(default)* | ollama.com | `OLLAMA_API_KEY` |
| `openrouter` | openrouter.ai | `OPENROUTER_API_KEY` |
| `deepseek` | api.deepseek.com | `DEEPSEEK_API_KEY` |
| `groq` | api.groq.com | `GROQ_API_KEY` |
| `local` | localhost:11434 | *(none)* |

Anything else: set `TOKENSAVE_BASE_URL` and `TOKENSAVE_API_KEY` directly.
With `local` your code never leaves the machine.

The transport is the plain OpenAI SDK pointed at a `base_url`, so any
OpenAI-compatible endpoint works. The measurements below were taken against
Ollama Cloud; the other presets are configured and exercised by the test suite
but their numbers will differ with the model you pick.

</details>

---

## The part nobody else does: enforcement

Every token-saving tool has the same failure mode — **the agent forgets to use
it**. A tool the model may ignore gets ignored, and your savings are whatever
the model felt like that day.

`token-save-mcp install-hook` registers a `PreToolUse` hook that **blocks**
`Read` on files over the threshold and redirects the agent to `bulk_read`:

```
Read("src/big_service.py")
→ BLOCKED: This file is 4,014 lines (threshold: 350).
  Use bulk_read to delegate this read instead.
  Need exact content to EDIT? Re-read with offset/limit — that passes through.
```

What still passes through, by design:

- **Targeted reads** (`offset`/`limit`) — editing needs exact text
- **Small files** — under the threshold, delegating costs more than it saves
- **Binaries** and missing files — nothing to summarise

Enforcement is **opt-in** and reversible: `token-save-mcp uninstall-hook`.

> The hook is Claude Code only. The `bulk_read` / `code_write` tools are plain
> MCP and work in any client — Cursor, Cline, Windsurf, Codex — just without
> the enforcement layer.

---

## Tools

### `bulk_read(question, paths, model?, effort?)`

Read files without pulling them into context.

```
bulk_read(
  question="Which methods touch the database, and where is auth enforced?",
  paths=["src/service.py", "src/handlers.py"]
)
```

**Use for:** surveying unfamiliar code, "what does this do", tracing a flow
across files, finding where something is handled.

**Don't use for:** editing (you need exact text — use a targeted read),
debugging that needs your own reasoning over raw code, or files under ~350
lines where delegation overhead exceeds the saving. The tool tells you when
you've crossed that line rather than silently burning a call.

### `code_write(spec, reference, target?, model?, effort?)`

Generate boilerplate matching an existing file's style. With `target`, the code
is written **straight to disk** and only a confirmation returns — the generated
code never enters your context at all.

```
code_write(
  spec="pytest suite for clamp(value, lo, hi), covering both bounds and lo>hi",
  reference=["tests/test_total.py"],
  target="tests/test_clamp.py"
)
→ Wrote tests/test_clamp.py (32 lines). Not read into your context.
```

Never overwrites: the target is created with `O_EXCL`, which also refuses to
follow a dangling symlink.

### `status()`

Prints the live configuration and makes one tiny call to prove the worker is
actually reachable.

---

## Measured savings

Real runs, not projections. Each number is the footer from an actual call:

| What | Size | Direct read | Via token-save | Saved |
|---|---|---|---|---|
| One large TS file | 602 lines | ≈13,340 tok | ≈689 tok | **95%** |
| Production Python | 443 lines | ≈5,788 tok | ≈684 tok | **88%** |
| 4 files, 102 KB | 1,910 lines | ≈28,379 tok | ≈304 tok | **99%** |
| Code generation to disk | 58 lines out | — | 0 tok | **100%** |

**Method:** "direct read" is the file's own size at ~3.6 chars/token (source
code is denser than prose); "via token-save" is the returned answer measured the
same way. The worker's in/out numbers come from the provider's `usage` field.
Reproduce any row by running the same call — the footer prints on every one.

**Where it's weaker, honestly:** on a 281-line diff the saving was 67%, because
a short input with a long answer is the worst case. The tool says so in its own
output. Savings are best where the file is big and the question is narrow.

---

## How it compares

Different tools solve "too many tokens" in genuinely different ways:

| | Approach | Enforced? | Savings figure |
|---|---|---|---|
| **token-save-mcp** | LLM worker reads, returns an answer | **Yes** — hook blocks Read | Measured per call |
| Static AST tools | Parse the tree, return exact symbols | No | Deterministic |
| Other delegation MCPs | LLM worker, single provider | No | Usually estimated |

**Static AST tools are better than this one** at "give me the exact body of
`handleRequest`" — they're free, instant, and can't hallucinate. Reach for them
for symbol lookup.

This tool is for **semantic questions over large files** — "what does this
service do", "where does auth happen", "which of these files handle retries" —
where you want an answer, not an extract. That costs a worker call and a few
seconds, and a worker can be wrong. Use both.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TOKENSAVE_PROVIDER` | `ollama` | Preset: ollama, openrouter, deepseek, groq, local |
| `TOKENSAVE_API_KEY` | — | Overrides the preset's key variable |
| `TOKENSAVE_BASE_URL` | preset | Any OpenAI-compatible endpoint |
| `TOKENSAVE_MODEL` | preset | Worker model id |
| `TOKENSAVE_MIN_LINES` | `350` | Hook threshold, and the "too small" warning |
| `TOKENSAVE_HOOK_MAX_BYTES` | `100000` | Also block on size — catches minified files |
| `TOKENSAVE_MAX_CORPUS_BYTES` | `2000000` | Ceiling on one request |
| `TOKENSAVE_TIMEOUT` | `600` | Seconds per call |
| `TOKENSAVE_MAX_RETRIES` | `4` | Retries on transient failures |
| `TOKENSAVE_MAX_CONCURRENCY` | `3` | Match your provider's limit |

---

## When not to use this

Being clear about this is the point, not a disclaimer:

- **You need exact text to edit.** Use a targeted read. The hook lets those through.
- **You're debugging subtle behaviour.** Summaries lose the detail that matters.
- **The file is small.** Under ~350 lines, reading directly is cheaper and faster.
- **The worker can be wrong.** It's an LLM. For anything you'll act on blindly,
  verify against the source. Static tools don't have this failure mode.

---

## Development

```bash
git clone https://github.com/Habartru/token_save_mcp
cd token_save_mcp
pip install -e ".[dev]"

python tests/test_server.py    # 83 offline tests, no API calls
bash tests/test_hook.sh        # 16 hook routing tests
```

The test suite stubs the transport, so it costs nothing to run and is safe in
CI. It covers the retry loop, corpus assembly, fence stripping, the disk-write
guards, and every hook routing decision.

---

## Credits

The delegation-plus-hook pattern is adapted from the `shunt` plugin in
[spotify/portal-ai-plugins](https://github.com/spotify/portal-ai-plugins)
(Apache-2.0), which routes the same kind of work through Spotify's internal
Portal CLI. This project keeps the idea and swaps the transport for any
OpenAI-compatible provider, so no corporate Portal instance is required. Files
also travel in-process rather than through `argv`, which removes the 128 KiB
per-argument limit on Linux.

MIT licensed.
