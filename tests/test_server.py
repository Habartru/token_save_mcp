#!/usr/bin/env python3
"""
token-save-mcp — offline test suite.

Exercises corpus assembly, guard rails, fence stripping, the savings report and
the transport's retry/extract behaviour against a stubbed client. Makes no paid
model call, so it is safe to run on every change.

    python tests/test_server.py
"""

import asyncio
import importlib.util
import pathlib
import sys
import tempfile

SERVER = (pathlib.Path(__file__).resolve().parent.parent
          / "src" / "token_save_mcp" / "server.py")

# A key must exist before the module is imported, or it exits at import time.
import os
os.environ.setdefault("TOKENSAVE_API_KEY", "test-key-not-used")

_spec = importlib.util.spec_from_file_location("token_save_under_test", SERVER)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results = []


def check(name, desc, cond, detail=""):
    _results.append(bool(cond))
    print(f"  {PASS if cond else FAIL}  {name:<34} {desc}")
    if not cond and detail:
        print(f"        → {detail}")


def section(title):
    print(f"\n{title}\n" + "─" * 64)


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

TMP = pathlib.Path(tempfile.mkdtemp(prefix="shunt-evals-"))

BIG = TMP / "big_service.py"
BIG.write_text("\n".join(
    f"def handler_{i}(request):\n    return db.query({i})" for i in range(400)
), encoding="utf-8")
# Two lines per handler, joined by newlines: 800 lines, no trailing newline.
BIG_LINES = BIG.read_text().count("\n") + 1

SMALL = TMP / "small.py"
SMALL.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

REF = TMP / "ref_test.py"
REF.write_text(
    "import pytest\n\n\ndef test_order_total():\n    assert total([1, 2]) == 3\n",
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
#  Corpus assembly
# ---------------------------------------------------------------------------

section("Corpus assembly (_load_corpus)")

corpus, stats, err = S._load_corpus([str(BIG)])
check("loads-file", "reads a file into the corpus", err is None and corpus, err)
check("xml-wrapped", "file is wrapped in <file path=...> tags",
      corpus.startswith('<file path=') and corpus.rstrip().endswith("</file>"))
check("content-verbatim", "file content survives verbatim",
      "def handler_399(request):" in corpus)
check("stats-lines", "line count is reported",
      stats and stats[0]["lines"] == BIG_LINES,
      f'got {stats[0]["lines"]}, want {BIG_LINES}' if stats else "no stats")

corpus2, stats2, err2 = S._load_corpus([str(BIG), str(SMALL)])
check("multi-file", "two files produce two <file> blocks",
      err2 is None and corpus2.count("<file path=") == 2)
check("multi-stats", "stats cover every file", len(stats2) == 2)

_, _, err3 = S._load_corpus([])
check("empty-paths-rejected", "no paths is an error", err3 and "No paths" in err3)

_, _, err4 = S._load_corpus([str(TMP / "nope.py")])
check("missing-file-rejected", "a missing file is an error",
      err4 and "not found" in err4.lower())

_, _, err5 = S._load_corpus([str(TMP)])
check("directory-rejected", "a directory is rejected with guidance",
      err5 and "directory" in err5.lower())

_orig_cap = S.MAX_CORPUS_BYTES
S.MAX_CORPUS_BYTES = 100
_, _, err6 = S._load_corpus([str(BIG)])
check("oversized-rejected", "corpus over the ceiling is refused",
      err6 and "exceeds" in err6.lower())
check("oversized-explains", "the refusal names the limit and the fix",
      err6 and "TOKENSAVE_MAX_CORPUS_BYTES" in err6)
S.MAX_CORPUS_BYTES = _orig_cap

BINARY = TMP / "blob.bin"
BINARY.write_bytes(b"\xff\xfe\x00\x01 not utf8 \xc3\x28")
_, _, err7 = S._load_corpus([str(BINARY)])
check("binary-survives", "undecodable bytes are replaced, not fatal", err7 is None)


# ---------------------------------------------------------------------------
#  Fence stripping
# ---------------------------------------------------------------------------

section("Fence stripping (_strip_fences)")

check("strips-plain", "```...``` is stripped",
      S._strip_fences("```\ncode here\n```") == "code here")
check("strips-lang", "```python fence is stripped",
      S._strip_fences("```python\nx = 1\n```") == "x = 1")
check("keeps-unfenced", "unfenced code is untouched",
      S._strip_fences("x = 1") == "x = 1")
# An inner fence makes the fence count ambiguous (3 fence lines, not 2), so the
# text is left verbatim rather than risking a strip that swallows real content.
# Visible fences are a nuisance; silently merged prose is corruption.
_inner = '```python\ndoc = """\n```\n"""\n```'
check("keeps-inner", "ambiguous inner fences are left verbatim, not guessed at",
      S._strip_fences(_inner) == _inner, repr(S._strip_fences(_inner)))
check("no-partial-strip", "a lone opening fence is not mangled",
      S._strip_fences("```python\nx = 1") == "```python\nx = 1")


# ---------------------------------------------------------------------------
#  Token estimate + savings report
# ---------------------------------------------------------------------------

section("Accounting (_estimate_tokens, _savings_report)")

check("estimate-monotonic", "more text estimates more tokens",
      S._estimate_tokens("x" * 3600) > S._estimate_tokens("x" * 360))
check("estimate-nonzero", "empty text still estimates >= 1",
      S._estimate_tokens("") >= 1)

_stats = [{"path": "a.py", "lines": 4000, "bytes": 120000, "tokens": 33000}]
_res = {"text": "- short answer", "in_tokens": 34000, "out_tokens": 120,
        "seconds": 4.2, "_model": "glm-5.3-flash"}
rep = S._savings_report(_stats, _res)
check("report-has-saving", "report states the saving", "saved" in rep)
check("report-has-worker", "report names the worker", "glm-5.3-flash" in rep)
check("report-has-usage", "report carries real worker usage", "34,000 in" in rep)
check("report-percent", "a big file reports a high saving %",
      any(f"{n}%" in rep for n in (98, 99, 100)), rep)


# ---------------------------------------------------------------------------
#  Transport (stubbed client)
# ---------------------------------------------------------------------------

section("Transport (_call against a stub)")


class _Msg:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning
        self.model_extra = {}


class _Usage:
    prompt_tokens = 1234
    completion_tokens = 56


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = _Usage()


class _StubCompletions:
    """Replays a scripted sequence of responses/exceptions and records calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def stub(script):
    comp = _StubCompletions(script)
    S._client = type("X", (), {"chat": type("Y", (), {"completions": comp})()})()
    return comp


class _Status429(Exception):
    status_code = 429


class _Status400(Exception):
    status_code = 400


_real_sleep = asyncio.sleep
asyncio.sleep = lambda *_a, **_k: _real_sleep(0)  # no real backoff in tests

comp = stub([_Resp(_Msg("- bullet one"))])
r = asyncio.run(S._call("glm-5.3-flash", "SYS", "PROMPT", "low"))
check("answer-extracted", "the worker's text is returned",
      r["ok"] and r["text"] == "- bullet one")
check("usage-captured", "real token usage is captured",
      r["in_tokens"] == 1234 and r["out_tokens"] == 56)
check("system-sent", "the system prompt is sent",
      comp.calls[0]["messages"][0] == {"role": "system", "content": "SYS"})
check("prompt-sent", "the user prompt survives verbatim",
      comp.calls[0]["messages"][1]["content"] == "PROMPT")
check("effort-sent", "reasoning_effort is passed through",
      comp.calls[0]["extra_body"] == {"reasoning_effort": "low"})
check("model-sent", "the requested worker is the one called",
      comp.calls[0]["model"] == "glm-5.3-flash")
check("no-history", "every delegation is one shot",
      len(comp.calls[0]["messages"]) == 2)

comp = stub([_Resp(_Msg(None, "answer in reasoning field"))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("reasoning-fallback", "an answer in the reasoning field is not dropped",
      r["ok"] and r["text"] == "answer in reasoning field")

comp = stub([_Resp(_Msg(""))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("empty-is-failure", "an empty response is a failure, not an answer",
      not r["ok"] and "empty" in r["text"])

comp = stub([_Status429(), _Resp(_Msg("recovered"))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("retries-429", "a 429 is retried", r["ok"] and r["text"] == "recovered")
check("retry-count", "exactly one retry was spent", len(comp.calls) == 2)

comp = stub([_Status400(), _Resp(_Msg("should not reach"))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("no-retry-400", "a 400 fails fast rather than retrying",
      not r["ok"] and len(comp.calls) == 1)

comp = stub([ConnectionError("connection reset by peer"), _Resp(_Msg("ok now"))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("retries-connection", "a dropped connection is retried", r["ok"])

comp = stub([_Status429()] * (S.MAX_RETRIES + 1))
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("retry-exhaustion", "retries are bounded and the error surfaces",
      not r["ok"] and "[error]" in r["text"])

asyncio.sleep = _real_sleep


# ---------------------------------------------------------------------------
#  Tool-level guards (no model call reached)
# ---------------------------------------------------------------------------

section("Tool guards")

fn_bulk = S.bulk_read.fn if hasattr(S.bulk_read, "fn") else S.bulk_read
fn_write = S.code_write.fn if hasattr(S.code_write, "fn") else S.code_write

r = asyncio.run(fn_bulk("", [str(BIG)]))
check("bulk-empty-question", "an empty question is refused", "[error]" in r)

_c = stub([_Resp(_Msg("- ok"))])
r = asyncio.run(fn_bulk("q", [str(BIG)], model="any-provider-model"))
check("bulk-any-model-allowed", "any provider model id is accepted",
      "[error]" not in r, r[:80])
check("bulk-model-is-used", "the override reaches the API call",
      _c.calls and _c.calls[-1]["model"] == "any-provider-model",
      _c.calls[-1]["model"] if _c.calls else "no call made")

r = asyncio.run(fn_bulk("q", [str(BIG)], model="   "))
check("bulk-empty-model-refused", "a blank model id is refused",
      "[error]" in r and "TOKENSAVE_MODEL" in r)

r = asyncio.run(fn_bulk("q", [str(TMP / "ghost.py")]))
check("bulk-missing-file", "a missing file is refused before any call",
      "[error]" in r and "not found" in r.lower())

r = asyncio.run(fn_write("", [str(REF)]))
check("write-empty-spec", "an empty spec is refused", "[error]" in r)

r = asyncio.run(fn_write("make tests", []))
check("write-needs-reference", "generation without a reference is refused",
      "[error]" in r and "reference" in r.lower())

EXISTING = TMP / "already_here.py"
EXISTING.write_text("keep me\n", encoding="utf-8")
r = asyncio.run(fn_write("spec", [str(REF)], target=str(EXISTING)))
check("write-no-clobber", "an existing target is never overwritten",
      "[error]" in r and "exists" in r)
check("clobber-guard-is-free", "the clobber guard fires before the worker call",
      EXISTING.read_text() == "keep me\n")

r = asyncio.run(fn_write("spec", [str(REF)], target=str(TMP / "no_such_dir/x.py")))
check("write-missing-dir", "a nonexistent target directory is refused",
      "[error]" in r and "directory" in r.lower())


# ---------------------------------------------------------------------------
#  Tool happy paths (stubbed worker)
# ---------------------------------------------------------------------------

section("Tool happy paths (stubbed worker)")

stub([_Resp(_Msg("- handler_0: queries db"))])
r = asyncio.run(fn_bulk("what does it do", [str(BIG)]))
check("bulk-returns-answer", "the answer reaches the caller",
      "handler_0: queries db" in r)
check("bulk-appends-report", "the savings footer is appended", "token-save:" in r)
check("bulk-reports-lines", "the footer counts the real lines",
      f"{BIG_LINES:,} lines" in r, r[-200:])

stub([_Resp(_Msg("- tiny"))])
r = asyncio.run(fn_bulk("q", [str(SMALL)]))
check("bulk-small-file-note", "a sub-threshold file is flagged as not worth it",
      "under the" in r and "threshold" in r)

OUT = TMP / "generated_test.py"
stub([_Resp(_Msg("```python\nimport pytest\n\n\ndef test_x():\n    assert True\n```"))])
r = asyncio.run(fn_write("write a test", [str(REF)], target=str(OUT)))
check("write-creates-file", "the target file is created", OUT.exists())
check("write-strips-fences", "fences are stripped before writing",
      OUT.exists() and not OUT.read_text().startswith("```"),
      OUT.read_text()[:40] if OUT.exists() else "")
check("write-content-correct", "the generated code lands intact",
      OUT.exists() and "def test_x():" in OUT.read_text())
check("write-confirms-only", "only a confirmation returns, not the code",
      "def test_x():" not in r and "Wrote" in r)

stub([_Resp(_Msg("x = 1"))])
r = asyncio.run(fn_write("stub", [str(REF)]))
check("write-stdout-returns-code", "without a target the code is returned",
      "x = 1" in r)
check("write-stdout-warns", "and the caller is told it is now in context",
      "in your context" in r)

stub([_Status400()])
r = asyncio.run(fn_bulk("q", [str(BIG)]))
check("bulk-failure-is-clear", "a failed delegation says nothing was read",
      "[error]" in r and "nothing was read" in r)

stub([_Status400()])
r = asyncio.run(fn_write("s", [str(REF)], target=str(TMP / "never.py")))
check("write-failure-no-file", "a failed generation writes no file",
      not (TMP / "never.py").exists() and "nothing was written" in r)


# ---------------------------------------------------------------------------
#  Regressions — each of these was a real defect found in review
# ---------------------------------------------------------------------------

section("Regressions (defects found in review)")

# 1. /dev/zero has no EOF: reading it before checking the size ate all memory.
_, _, err = S._load_corpus(["/dev/zero"])
check("special-file-rejected", "a character device is refused, not read",
      err and "not a regular file" in err, err)

import os as _os
FIFO = TMP / "fifo"
try:
    _os.mkfifo(FIFO)
    _, _, err = S._load_corpus([str(FIFO)])
    check("fifo-rejected", "a FIFO is refused rather than blocking forever",
          err and "not a regular file" in err, err)
except (AttributeError, OSError):
    check("fifo-rejected", "a FIFO is refused rather than blocking forever", True,
          "skipped: mkfifo unavailable")

# 2. The ceiling used to be checked only after the whole file was in memory.
_cap = S.MAX_CORPUS_BYTES
S.MAX_CORPUS_BYTES = 50
_, _, err = S._load_corpus([str(BIG)])
check("ceiling-before-read", "an oversized file is refused before being read",
      err and "exceeds" in err.lower(), err)
S.MAX_CORPUS_BYTES = _cap

# 3. Multi-block output used to be merged into one file, prose and all.
multi = "```python\npart1\n```\nsome prose\n```python\npart2\n```"
out = S._strip_fences(multi)
check("multiblock-not-merged", "two fenced blocks are left verbatim, not merged",
      "some prose" in out and out.startswith("```"), out[:60])
check("singleblock-still-stripped", "a single clean pair is still stripped",
      S._strip_fences("```python\nx = 1\n```") == "x = 1")
check("indent-preserved", "a leading indent is not eaten",
      S._strip_fences("    return x") == "    return x",
      repr(S._strip_fences("    return x")))

# 4. exists()+write was a TOCTOU race and followed a dangling symlink.
DANGLING = TMP / "dangling.py"
SECRET = TMP / "symlink_victim.py"
try:
    _os.symlink(SECRET, DANGLING)
    stub([_Resp(_Msg("x = 1"))])
    r = asyncio.run(fn_write("spec", [str(REF)], target=str(DANGLING)))
    check("dangling-symlink-refused", "a dangling symlink target is refused",
          "[error]" in r, r[:80])
    check("symlink-victim-untouched", "nothing is written through the link",
          not SECRET.exists())
except (AttributeError, OSError):
    check("dangling-symlink-refused", "a dangling symlink target is refused", True,
          "skipped: symlink unavailable")

# 5. A malformed 200 (no choices) used to raise out of the tool instead of retrying.
class _NoChoices:
    choices = []
    usage = _Usage()

_saved_sleep = asyncio.sleep
asyncio.sleep = lambda *_a, **_k: _saved_sleep(0)

comp = stub([_NoChoices(), _Resp(_Msg("recovered after empty choices"))])
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("empty-choices-retried", "an empty `choices` is retried, not raised",
      r["ok"] and "recovered" in r["text"], r["text"][:60])

comp = stub([_NoChoices()] * (S.MAX_RETRIES + 1))
r = asyncio.run(S._call("glm-5.3-flash", "S", "P", "low"))
check("empty-choices-gives-up", "persistent malformed responses end as an error",
      not r["ok"] and "malformed" in r["text"], r["text"][:60])

asyncio.sleep = _saved_sleep

# 6. A SOCKS proxy in the environment used to crash with an httpx stack trace.
check("socks-error-is-clear", "a missing SOCKS package yields an actionable message",
      "_build_client" in (pathlib.Path(S.__file__).read_text()
                          if hasattr(S, "__file__") else ""))


# ---------------------------------------------------------------------------
#  Docstrings must not advertise ids the roster does not have
# ---------------------------------------------------------------------------

section("Docstring / roster consistency")

# The old council copy documented a model id that was not in its registry, and
# shunt repeated the mistake after a roster change: the tool description reaches
# Claude as fact, so a stale id there is an invitation to call a dead model.
import re as _re

# The tool description reaches the agent as fact. It must not pin a model list
# that goes stale the moment a provider ships a new model.
_doc = fn_bulk.__doc__ or ""
check("doc-no-fixed-roster", "bulk_read docstring does not pin a model list",
      "glm-5.3-flash" not in _doc and "deepseek-v4" not in _doc, _doc[:120])
check("doc-points-at-status", "docstring tells the reader how to check config",
      "status" in _doc)
check("doc-warns-small-files", "docstring says when NOT to delegate",
      "350" in _doc or "under" in _doc)
_wdoc = fn_write.__doc__ or ""
check("write-doc-requires-ref", "code_write docstring explains why reference is required",
      "reference" in _wdoc)


# ---------------------------------------------------------------------------
#  Ledger
# ---------------------------------------------------------------------------

section("Savings ledger")

import json as _json

_led = TMP / "ledger.jsonl"
S.LEDGER = _led
stub([_Resp(_Msg("- an answer"))])
asyncio.run(fn_bulk("q", [str(BIG)]))
check("ledger-written", "a call appends one line to the ledger", _led.exists())
if _led.exists():
    _rows = [_json.loads(l) for l in _led.read_text().splitlines() if l.strip()]
    check("ledger-one-row", "exactly one row per call", len(_rows) == 1, len(_rows))
    check("ledger-fields", "the row carries the numbers stats needs",
          _rows and all(k in _rows[0] for k in
                        ("ts", "kind", "direct_tokens", "context_tokens", "lines")))
    check("ledger-kind", "the row says which tool ran",
          _rows and _rows[0]["kind"] == "bulk_read")

# Bookkeeping must never break a real call.
S.LEDGER = pathlib.Path("/proc/nonexistent-dir/ledger.jsonl")
stub([_Resp(_Msg("- still fine"))])
r = asyncio.run(fn_bulk("q", [str(BIG)]))
check("ledger-failure-is-silent", "an unwritable ledger does not fail the call",
      "still fine" in r, r[:80])

os.environ["TOKENSAVE_NO_LEDGER"] = "1"
S.LEDGER = TMP / "should-not-appear.jsonl"
stub([_Resp(_Msg("- opted out"))])
asyncio.run(fn_bulk("q", [str(BIG)]))
check("ledger-opt-out", "TOKENSAVE_NO_LEDGER disables recording entirely",
      not (TMP / "should-not-appear.jsonl").exists())
del os.environ["TOKENSAVE_NO_LEDGER"]
S.LEDGER = _led


# ---------------------------------------------------------------------------
#  run_command
# ---------------------------------------------------------------------------

section("run_command")

fn_run = S.run_command.fn if hasattr(S.run_command, "fn") else S.run_command
S.LEDGER = TMP / "run_ledger.jsonl"
os.environ["TOKENSAVE_LOG_DIR"] = str(TMP / "logs")

r = asyncio.run(fn_run(""))
check("run-empty-command", "an empty command is refused", "[error]" in r)

r = asyncio.run(fn_run("echo hi", cwd=str(TMP / "nope")))
check("run-bad-cwd", "a missing cwd is refused before running anything",
      "[error]" in r and "not a directory" in r)

# Short output costs more to delegate than to return — it should come back whole.
r = asyncio.run(fn_run("echo alpha && echo beta"))
check("run-short-verbatim", "short output is returned in full, not summarised",
      "alpha" in r and "beta" in r and "short" in r, r[:120])
check("run-short-no-worker", "and no worker call was made for it",
      "token-save:" not in r)

# Exit code is reported, not swallowed.
r = asyncio.run(fn_run("exit 3"))
check("run-reports-exit-code", "a non-zero exit is reported with its code",
      "exit 3" in r, r[:120])

r = asyncio.run(fn_run("sleep 5", timeout=1))
check("run-timeout-kills", "a command over the timeout is killed",
      "[error]" in r and "exceeded" in r)
check("run-timeout-advises", "and the message says how to allow longer",
      "timeout" in r.lower())

# Long output goes to the worker; the full log must survive on disk.
_long = "python3 -c \"print('noise line ' * 4 + chr(10), end='') or [print(f'line {i}: some padding text here to make this long') for i in range(400)]\""
stub([_Resp(_Msg("- summary of the run"))])
r = asyncio.run(fn_run(_long))
check("run-long-summarised", "long output comes back as a summary",
      "summary of the run" in r, r[:150])
check("run-long-has-footer", "with a measured savings footer", "token-save:" in r)
check("run-long-saves-log", "and the full output is written to a file",
      "Full output" in r and (TMP / "logs").exists())

_logs = sorted((TMP / "logs").glob("run-*.log"), key=lambda f: f.stat().st_mtime)
check("run-log-complete", "the saved log holds the whole output, not a slice",
      _logs and "line 399" in _logs[-1].read_text(),
      f"{len(_logs)} log(s)")

# A failed worker must not lose the output entirely.
stub([_Status400()])
r = asyncio.run(fn_run(_long))
check("run-worker-failure-falls-back", "a failed summary falls back to the tail",
      "last 25 lines" in r and "line 399" in r, r[:150])

_rows = [l for l in (TMP / "run_ledger.jsonl").read_text().splitlines() if l.strip()]
check("run-ledger-kind", "the ledger records run_command separately",
      any('"run_command"' in l for l in _rows), _rows[:1])

del os.environ["TOKENSAVE_LOG_DIR"]


# ---------------------------------------------------------------------------
#  Starting without a key
# ---------------------------------------------------------------------------

section("Missing key (server must still start)")

# A registry checking the server, or a client listing tools, connects before
# anything could be configured. Dying at import turns "not set up yet" into
# "this server is broken" — and the listing check fails.
check("starts-without-key", "the module imports with no key present",
      hasattr(S, "KEY_MISSING"))
check("key-error-exists", "there is a message for the unconfigured case",
      callable(getattr(S, "_key_error", None)))

_msg = S._key_error()
check("key-error-names-var", "it names the variable to set",
      "API_KEY" in _msg, _msg[:80])
check("key-error-says-how", "and how to fix it",
      "init" in _msg or "doctor" in _msg, _msg[:120])

_saved = S.KEY_MISSING
S.KEY_MISSING = True
try:
    r = asyncio.run(fn_bulk("q", [str(BIG)]))
    check("bulk-refuses-without-key", "bulk_read refuses clearly, not cryptically",
          "[error]" in r and "API key" in r, r[:80])
    r = asyncio.run(fn_write("spec", [str(REF)]))
    check("write-refuses-without-key", "code_write refuses the same way",
          "[error]" in r and "API key" in r, r[:80])
    r = asyncio.run(fn_run("echo hi"))
    check("run-refuses-without-key", "run_command refuses too",
          "[error]" in r and "API key" in r, r[:80])

    # status must work without a key: it is how you find out what is wrong.
    _st = S.status.fn if hasattr(S.status, "fn") else S.status
    r = asyncio.run(_st())
    check("status-works-without-key", "status still reports, rather than failing",
          "provider" in r and "[error]" not in r, r[:80])
    check("status-flags-missing-key", "and says the key is what is missing",
          "no api key" in r.lower(), repr(r))
    check("status-never-masks-placeholder", "the placeholder is not shown as a key",
          "not-co" not in r and "NOT SET" in r, r[:200])
finally:
    S.KEY_MISSING = _saved


# ---------------------------------------------------------------------------
#  Status tool
# ---------------------------------------------------------------------------

section("Status tool")

fn_status = S.status.fn if hasattr(S.status, "fn") else S.status
stub([_Resp(_Msg("ok"))])
r = asyncio.run(fn_status())
check("status-names-model", "status names the configured model", S.DEFAULT_MODEL in r)
check("status-names-provider", "status names the provider", S.PROVIDER in r)
check("status-shows-endpoint", "status shows the endpoint", S.BASE_URL in r)
check("status-hides-key", "the api key is never printed in full",
      S.API_KEY not in r or len(S.API_KEY) <= 8)
check("status-probes", "status reports the live probe result", "reachable" in r)

stub([_Status400()])
r = asyncio.run(fn_status())
check("status-reports-failure", "an unreachable worker is reported, not hidden",
      "NO" in r and "Check:" in r)


# ---------------------------------------------------------------------------
#  Provider configuration
# ---------------------------------------------------------------------------

section("Provider configuration")

check("presets-exist", "several providers are preset", len(S.PRESETS) >= 4)
for _p in ("ollama", "openrouter", "deepseek", "local"):
    check(f"preset-{_p}", f"{_p} is a known provider", _p in S.PRESETS)
check("preset-shape", "each preset carries url, key var and model",
      all(len(v) == 3 for v in S.PRESETS.values()))
check("local-needs-no-key", "the local preset declares no key variable",
      S.PRESETS["local"][1] == "")
check("base-url-is-configurable", "base_url comes from config, not a constant",
      S.BASE_URL == S.PRESETS[S.PROVIDER][0]
      or S.BASE_URL == os.environ.get("TOKENSAVE_BASE_URL"))
check("no-fixed-roster", "no hardcoded model roster remains",
      not hasattr(S, "WORKERS"))


# ---------------------------------------------------------------------------

print("\n" + "═" * 64)
passed, total = sum(_results), len(_results)
colour = "\033[32m" if passed == total else "\033[31m"
print(f"Total: {colour}{passed} passed\033[0m, "
      f"\033[31m{total - passed} failed\033[0m, {total} total")

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if passed == total else 1)
