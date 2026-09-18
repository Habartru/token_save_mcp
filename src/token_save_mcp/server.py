#!/usr/bin/env python3
"""
token-save-mcp — keep big files out of your agent's context.

An MCP server that delegates I/O-heavy work to a cheap worker model. The file
bytes are paid for once, in the *worker's* context; only the answer enters the
agent's. A 4,000-line file costs ~35K tokens to read and ~1K tokens as an answer.

Two tools plus a hook:

  bulk_read   files -> worker -> a short, structured answer
  code_write  spec + reference -> worker -> generated code, optionally to disk
  hook        (Claude Code only) blocks Read on large files and redirects here

What makes this different from "please remember to use the cheap tool":

  * Enforcement. The PreToolUse hook *blocks* an expensive Read instead of
    hoping the agent remembers. Targeted reads (offset/limit) pass through,
    because editing needs exact text.
  * A receipt, not an estimate. Every answer carries the worker's real token
    usage from the API response, next to the cost of the read it replaced.
  * Provider-agnostic. Any OpenAI-compatible endpoint: Ollama Cloud, OpenRouter,
    DeepSeek, a local Ollama or llama.cpp. One env var switches it.

Delegation is one-shot and stateless: a follow-up question re-sends the files.
That re-send is paid by the worker, not by your context.

Homepage: https://github.com/Habartru/token_save_mcp
License: MIT
"""

import asyncio
import os
import pathlib
import re
import time

from openai import AsyncOpenAI

# mcp 2.x renamed FastMCP to MCPServer. The decorator and run() surface we use
# is the same on both, so support either rather than pinning users to one.
try:
    from mcp.server.mcpserver import MCPServer as _Server   # mcp >= 2.0
except ImportError:  # pragma: no cover - depends on the installed sdk
    from mcp.server.fastmcp import FastMCP as _Server       # mcp 1.x

# ============================================================================
#  Config — provider-agnostic, driven entirely by environment variables
# ============================================================================

# Presets exist so the common cases need one variable, not three. Any other
# OpenAI-compatible endpoint works by setting TOKENSAVE_BASE_URL directly.
PRESETS = {
    "ollama": ("https://ollama.com/v1", "OLLAMA_API_KEY", "glm-5.3-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   "deepseek/deepseek-chat"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
             "llama-3.3-70b-versatile"),
    # Local runtimes: no key needed, but the OpenAI SDK insists on a non-empty one.
    "local": ("http://localhost:11434/v1", "", "qwen2.5-coder:7b"),
}

PROVIDER = os.environ.get("TOKENSAVE_PROVIDER", "ollama").strip().lower()
if PROVIDER not in PRESETS:
    raise SystemExit(
        f"TOKENSAVE_PROVIDER={PROVIDER!r} is not one of {sorted(PRESETS)}.\n"
        f"For any other OpenAI-compatible endpoint, set TOKENSAVE_BASE_URL "
        f"and TOKENSAVE_API_KEY instead."
    )

_preset_url, _preset_key_var, _preset_model = PRESETS[PROVIDER]

BASE_URL = os.environ.get("TOKENSAVE_BASE_URL", _preset_url)

# Key resolution: explicit override first, then the preset's own variable.
# A local runtime needs no key, but the OpenAI SDK rejects an empty string.
API_KEY = os.environ.get("TOKENSAVE_API_KEY", "")
if not API_KEY and _preset_key_var:
    API_KEY = os.environ.get(_preset_key_var, "")
if not API_KEY:
    if PROVIDER == "local" or BASE_URL.startswith(("http://localhost",
                                                   "http://127.0.0.1")):
        API_KEY = "not-needed"
    else:
        raise SystemExit(
            f"No API key. Set {_preset_key_var or 'TOKENSAVE_API_KEY'} "
            f"(provider: {PROVIDER}).\n"
            f"Run `token-save-mcp doctor` to check your configuration."
        )

# The worker model. Unlike a fixed roster, any model id the provider serves is
# allowed — a hardcoded list goes stale the moment a provider ships a new one.
DEFAULT_MODEL = os.environ.get("TOKENSAVE_MODEL", _preset_model)

TIMEOUT = float(os.environ.get("TOKENSAVE_TIMEOUT", "600"))
MAX_RETRIES = int(os.environ.get("TOKENSAVE_MAX_RETRIES", "4"))
MAX_CONCURRENCY = int(os.environ.get("TOKENSAVE_MAX_CONCURRENCY", "3"))

# Ceiling on one request's corpus. Files are read in-process, so this protects
# the worker's context window rather than any OS argv limit.
MAX_CORPUS_BYTES = int(os.environ.get("TOKENSAVE_MAX_CORPUS_BYTES", str(2_000_000)))

# Below this, delegation overhead outweighs the saving and the tool says so
# rather than silently burning a worker call.
MIN_LINES = int(os.environ.get("TOKENSAVE_MIN_LINES", "350"))

def _build_client() -> AsyncOpenAI:
    """Construct the client, turning the two common environment failures into
    messages that say what to do instead of a stack trace from deep in httpx."""
    try:
        return AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=TIMEOUT)
    except ImportError as exc:
        if "socks" in str(exc).lower():
            proxy = next(
                (os.environ[v] for v in ("ALL_PROXY", "all_proxy",
                                         "HTTPS_PROXY", "https_proxy")
                 if os.environ.get(v)), "(unset)"
            )
            raise SystemExit(
                f"A SOCKS proxy is set in your environment ({proxy.split('://')[0]}://…) "
                f"but the SOCKS support package is missing.\n"
                f"Either install it:  pip install 'httpx[socks]'\n"
                f"or exclude this host from the proxy via NO_PROXY."
            ) from None
        raise


_client = _build_client()
_sem = asyncio.Semaphore(MAX_CONCURRENCY)

mcp = _Server("token-save")


# ============================================================================
#  Worker instructions
# ============================================================================

BULK_READER_SYSTEM = (
    "You are a precise code analyst. Read the provided files and answer the "
    "question concisely. Output structured bullets only. No greetings, no prose, "
    "no preambles, no summaries of what you are about to do. Lead every bullet "
    "with the exact identifier, type, or line number. Use nested bullets for "
    "detail. Cite file paths and line numbers wherever you make a claim about "
    "the code. Skip anything the caller did not ask for. If the files do not "
    "contain the answer, say exactly that rather than guessing."
)

CODE_WRITER_SYSTEM = (
    "You generate code files from a spec and reference files. Match the existing "
    "patterns, conventions, naming, imports, and style of the reference exactly. "
    "Output only the code — no explanations, no commentary, no markdown fences. "
    "If the spec is ambiguous, make the choice that best matches the reference."
)


# ============================================================================
#  Transport — mirrors the council server's retry/extract behaviour
# ============================================================================

def _is_retryable(exc: Exception) -> bool:
    """Transient (retry) vs fatal (fail fast). A 4xx other than 429 is the
    caller's fault; connection/SSL/timeouts and 5xx are worth another attempt."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(s in msg for s in (
        "ssl", "eof", "timeout", "timed out", "connection reset",
        "connection aborted", "connection error", "temporarily unavailable",
        "bad gateway", "service unavailable", "apiconnectionerror",
    ))


def _extract_text(msg) -> str:
    """Reasoning models sometimes return empty `content` with the answer in a
    separate reasoning field — fall back to it so a real answer is never lost."""
    text = (getattr(msg, "content", None) or "").strip()
    if text:
        return text
    extra = getattr(msg, "model_extra", None) or {}
    for k in ("reasoning_content", "reasoning"):
        v = getattr(msg, k, None) or extra.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


async def _call(model: str, system: str, prompt: str, effort: str) -> dict:
    """One ephemeral turn against a worker. Returns text plus real token usage."""
    last_err = "unknown error"
    started = time.monotonic()

    for attempt in range(MAX_RETRIES + 1):
        async with _sem:
            try:
                resp = await _client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    extra_body={"reasoning_effort": effort} if effort else None,
                )
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                if not (attempt < MAX_RETRIES and _is_retryable(exc)):
                    return {"ok": False, "text": f"[error] {last_err}",
                            "in_tokens": 0, "out_tokens": 0,
                            "seconds": time.monotonic() - started}
            else:
                # Parsing lives inside the attempt, not after it: a gateway can
                # answer 200 with an empty `choices`, and that is a transient
                # fault worth retrying — not an exception thrown at the caller.
                try:
                    choices = getattr(resp, "choices", None) or []
                    if not choices:
                        raise ValueError("response carried no choices")
                    text = _extract_text(choices[0].message)
                except Exception as exc:
                    last_err = f"malformed response: {type(exc).__name__}: {exc}"
                    if attempt >= MAX_RETRIES:
                        return {"ok": False, "text": f"[error] {last_err}",
                                "in_tokens": 0, "out_tokens": 0,
                                "seconds": time.monotonic() - started}
                    # Otherwise fall through to the shared backoff below, which
                    # sleeps OUTSIDE the semaphore so a retry holds no slot.
                else:
                    usage = getattr(resp, "usage", None)
                    return {
                        "ok": bool(text),
                        "text": text or "[error] worker returned an empty response",
                        "in_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                        "out_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                        "seconds": time.monotonic() - started,
                    }
        # Back off OUTSIDE the semaphore so a sleeping retry frees its slot.
        await asyncio.sleep(min(2 ** attempt, 20))

    return {"ok": False, "text": f"[error] {last_err}", "in_tokens": 0,
            "out_tokens": 0, "seconds": time.monotonic() - started}


# ============================================================================
#  Corpus assembly
# ============================================================================

def _estimate_tokens(text: str) -> int:
    """Rough token estimate for the savings report. Deliberately conservative:
    ~3.6 chars/token is typical for source code, which is denser than prose."""
    return max(1, round(len(text) / 3.6))


def _load_corpus(paths: list[str]) -> tuple[str, list[dict], str | None]:
    """Read files into one XML-tagged blob. Returns (corpus, stats, error)."""
    if not paths:
        return "", [], "No paths given. Pass at least one file path."

    chunks: list[str] = []
    stats: list[dict] = []
    total = 0

    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        if not p.exists():
            return "", [], f"File not found: {raw}"
        if p.is_dir():
            return "", [], (
                f"{raw} is a directory. Pass individual file paths "
                f"(use Glob/Grep to find them first)."
            )
        # Only regular files. A FIFO blocks forever on open and a character
        # device like /dev/zero has no EOF — read_text() would eat all memory.
        if not p.is_file():
            return "", [], (
                f"{raw} is not a regular file (device, FIFO or socket). "
                f"Pass ordinary files."
            )

        # Check the on-disk size BEFORE reading, so an oversized file is refused
        # rather than loaded into memory and then rejected.
        try:
            on_disk = p.stat().st_size
        except OSError as exc:
            return "", [], f"Cannot stat {raw}: {type(exc).__name__}: {exc}"
        if total + on_disk > MAX_CORPUS_BYTES:
            return "", [], (
                f"Corpus exceeds {MAX_CORPUS_BYTES} bytes at {raw}. "
                f"Send fewer files, or raise TOKENSAVE_MAX_CORPUS_BYTES."
            )

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return "", [], f"Cannot read {raw}: {type(exc).__name__}: {exc}"

        # Re-check after decoding: errors="replace" turns each bad byte into a
        # 3-byte U+FFFD, and a /proc-style file reports st_size 0 but has content.
        size = len(text.encode("utf-8"))
        total += size
        if total > MAX_CORPUS_BYTES:
            return "", [], (
                f"Corpus exceeds {MAX_CORPUS_BYTES} bytes at {raw}. "
                f"Send fewer files, or raise TOKENSAVE_MAX_CORPUS_BYTES."
            )

        lines = text.count("\n") + 1
        stats.append({"path": str(p), "lines": lines, "bytes": size,
                      "tokens": _estimate_tokens(text)})
        # XML tags give the worker unambiguous file boundaries.
        chunks.append(f'<file path="{p}">\n{text}\n</file>')

    return "\n\n".join(chunks), stats, None


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Workers wrap code in markdown fences despite instructions. Strip exactly
    one wrapping fence pair.

    Only a single pair is stripped. With two or more pairs the regex would span
    from the first fence to the last and swallow the prose between them into the
    file — so anything more complex is written verbatim, fences and all. Visible
    fences in the output are a nuisance; silently merged prose is corruption.

    Leading whitespace is never stripped: the first line may be legitimately
    indented, and eating that indentation breaks Python.
    """
    candidate = text.strip()
    fence_lines = [ln for ln in candidate.splitlines() if ln.lstrip().startswith("```")]
    if len(fence_lines) == 2:
        m = _FENCE_RE.match(candidate)
        if m:
            return m.group(1)
    # No clean wrapping pair: keep the text as-is, trimming only trailing space.
    return text.rstrip()


def _savings_report(stats: list[dict], result: dict) -> str:
    """The honest accounting: what the direct read would have cost Claude vs
    what the delegation actually cost it."""
    direct = sum(s["tokens"] for s in stats)
    lines = sum(s["lines"] for s in stats)
    into_claude = _estimate_tokens(result["text"])
    saved = direct - into_claude
    pct = (saved / direct * 100) if direct else 0.0

    return (
        f"\n\n---\n"
        f"token-save: {len(stats)} file(s), {lines:,} lines | "
        f"direct read ≈{direct:,} tok → into Claude ≈{into_claude:,} tok "
        f"(saved ≈{saved:,}, {pct:.0f}%)\n"
        f"worker: {result['_model']} | {result['in_tokens']:,} in / "
        f"{result['out_tokens']:,} out | {result['seconds']:.1f}s"
    )


# ============================================================================
#  Tools
# ============================================================================

@mcp.tool()
async def bulk_read(
    question: str,
    paths: list[str],
    model: str = "",
    effort: str = "low",
) -> str:
    """Read large files WITHOUT pulling them into your context.

    Sends the files to a cheap worker model and returns only its answer. Use
    this instead of Read whenever you need to understand a large file or set of
    files but do not need their exact text.

    USE FOR: "what does this service do", "which methods hit the database",
    "where is X handled", "summarize this module's API", surveying unfamiliar
    code, tracing a flow across several files.

    DO NOT USE FOR: editing (you need exact content — use Read with
    offset/limit), debugging that needs your own reasoning over the raw code,
    files under ~350 lines (delegation overhead exceeds the saving), or
    architectural judgment calls.

    Every call is one-shot and stateless: a follow-up question re-sends the
    files. That re-send is paid by the worker, not by your context.

    Args:
        question: What to find out. Be specific — the worker sees only this
            and the files, and answers literally.
        paths: File paths to read. Directories are rejected; pass files.
        model: Worker model id, if you want to override the configured default.
            Any id your provider serves. Call `status` to see what is configured.
        effort: "low" | "medium" | "high". Low is right for mechanical
            extraction; raise it when the question needs real reasoning.

    Returns:
        The worker's answer plus a measured token-savings footer.
    """
    if not question.strip():
        return "[error] `question` is empty. The worker needs to know what to look for."

    worker = (model or DEFAULT_MODEL).strip()
    if not worker:
        return "[error] no model configured. Set TOKENSAVE_MODEL or pass `model`."

    corpus, stats, err = _load_corpus(paths)
    if err:
        return f"[error] {err}"

    total_lines = sum(s["lines"] for s in stats)
    prompt = (
        f"<question>{question}</question>\n\n"
        f"Answer the question using only these files:\n\n{corpus}"
    )

    result = await _call(worker, BULK_READER_SYSTEM, prompt, effort)
    result["_model"] = worker

    if not result["ok"]:
        return (
            f"{result['text']}\n\n"
            f"token-save: delegation failed, nothing was read into your context. "
            f"Fall back to Read (with offset/limit) if you need this now."
        )

    note = ""
    if total_lines < MIN_LINES:
        note = (
            f"\n\ntoken-save note: {total_lines} lines is under the {MIN_LINES}-line "
            f"threshold — a direct Read would likely have been cheaper here."
        )

    return result["text"] + note + _savings_report(stats, result)


@mcp.tool()
async def code_write(
    spec: str,
    reference: list[str],
    target: str = "",
    model: str = "",
    effort: str = "low",
) -> str:
    """Generate boilerplate code from a spec, matching an existing file's style.

    Sends the spec and reference file(s) to a worker model. With `target`, the
    generated code is written straight to disk and only a confirmation returns
    to your context — the generated code never enters it at all.

    USE FOR: test scaffolding from an existing test file, a new handler matching
    existing handlers, config stubs, repetitive CRUD, translation of a pattern
    across files.

    DO NOT USE FOR: code requiring real design judgment, subtle algorithms, or
    edits to existing files (this writes whole files).

    `reference` is required: without a file to match, the worker generates
    context-free code that fits nothing in the project.

    Args:
        spec: What to generate. Be concrete about names and behaviour.
        reference: File path(s) whose patterns and style to match.
        target: Where to write the result. If omitted, the code is returned to
            you (and therefore enters your context).
        model: Worker override. Defaults to glm-5.3-flash.
        effort: "low" | "medium" | "high".

    Returns:
        A write confirmation (with `target`), or the generated code.
    """
    if not spec.strip():
        return "[error] `spec` is empty. The worker needs to know what to generate."
    if not reference:
        return (
            "[error] `reference` is required — without a file to match patterns "
            "against, the worker generates code that fits nothing in the project."
        )

    worker = (model or DEFAULT_MODEL).strip()
    if not worker:
        return "[error] no model configured. Set TOKENSAVE_MODEL or pass `model`."

    corpus, stats, err = _load_corpus(reference)
    if err:
        return f"[error] {err}"

    # Refuse to clobber before spending a worker call, not after.
    target_path = None
    if target:
        target_path = pathlib.Path(target).expanduser()
        if target_path.exists():
            return (
                f"[error] {target} already exists. token-save does not overwrite; "
                f"delete it first or choose another target."
            )
        if not target_path.parent.exists():
            return f"[error] directory does not exist: {target_path.parent}"

    prompt = (
        f"<spec>{spec}</spec>\n\n"
        f"Match the patterns, conventions and style of these reference files:\n\n"
        f"{corpus}\n\n"
        f"Output only the generated code."
    )

    result = await _call(worker, CODE_WRITER_SYSTEM, prompt, effort)
    result["_model"] = worker

    if not result["ok"]:
        return f"{result['text']}\n\ntoken-save: generation failed, nothing was written."

    code = _strip_fences(result["text"])

    if target_path:
        # O_EXCL creates or fails — it never truncates. This closes both the
        # check-then-write race and the dangling-symlink hole, where exists()
        # reports False and a plain write would follow the link to a path the
        # caller never named.
        try:
            fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(code)
        except FileExistsError:
            return (
                f"[error] {target} already exists (or is a symlink). shunt does "
                f"not overwrite.\n\nThe generated code was not lost — re-run with "
                f"a different target, or omit `target` to have it returned here."
            )
        except Exception as exc:
            return (
                f"[error] generated {len(code):,} bytes but could not write to "
                f"{target}: {type(exc).__name__}: {exc}\n\n"
                f"Re-run without `target` to have the code returned instead."
            )

        written_lines = code.count("\n") + 1
        return (
            f"Wrote {target} ({written_lines:,} lines, {len(code):,} bytes).\n"
            f"The generated code was NOT read into your context — Read it "
            f"(or a slice of it) only if you need to review it.\n"
            f"\n---\n"
            f"token-save: worker {worker} | {result['in_tokens']:,} in / "
            f"{result['out_tokens']:,} out | {result['seconds']:.1f}s | "
            f"≈{_estimate_tokens(code):,} tok kept out of your context"
        )

    return (
        f"{code}\n\n---\n"
        f"token-save: worker {worker} | {result['in_tokens']:,} in / "
        f"{result['out_tokens']:,} out | {result['seconds']:.1f}s | "
        f"no --target, so this code IS now in your context"
    )


@mcp.tool()
async def status() -> str:
    """Report the current configuration and check the worker is reachable.

    Use when delegation is failing. Makes one tiny model call to prove the
    endpoint and key actually work.
    """
    masked = (API_KEY[:6] + "…") if len(API_KEY) > 8 else ("set" if API_KEY else "MISSING")
    lines = [
        "### token-save-mcp",
        "",
        f"provider       : {PROVIDER}",
        f"endpoint       : {BASE_URL}",
        f"worker model   : {DEFAULT_MODEL}",
        f"api key        : {masked}",
        f"line threshold : {MIN_LINES} (below this, a direct read is cheaper)",
        f"byte ceiling   : {MAX_CORPUS_BYTES:,} per request",
        f"timeout        : {TIMEOUT:.0f}s | retries {MAX_RETRIES} | concurrency {MAX_CONCURRENCY}",
        "",
    ]

    probe = await _call(DEFAULT_MODEL, "Reply with the single word: ok",
                        "Reply with the single word: ok", "low")
    if probe["ok"]:
        lines.append(f"worker reachable: YES ({probe['seconds']:.1f}s, "
                     f"{probe['in_tokens']} in / {probe['out_tokens']} out)")
    else:
        lines += [
            f"worker reachable: NO — {probe['text']}",
            "",
            "Check: is the key valid, is the model id served by this provider,",
            "and is TOKENSAVE_BASE_URL right? A local runtime must be running.",
        ]

    lines += [
        "",
        "tools: bulk_read (files -> answer), code_write (spec -> code/disk)",
        "Files are read in-process, so the only size ceiling is the worker's context.",
    ]
    return "\n".join(lines)


def main() -> None:
    """Console-script entry point: run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
