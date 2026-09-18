#!/usr/bin/env python3
"""
token-save-mcp — CLI tests.

Covers onboarding: what a brand-new user sees, how a provider is chosen, and
the exact shape of the `claude mcp add` command. Makes no network call.

    python tests/test_cli.py
"""

import importlib.util
import os
import pathlib
import sys

CLI = (pathlib.Path(__file__).resolve().parent.parent
       / "src" / "token_save_mcp" / "cli.py")
_spec = importlib.util.spec_from_file_location("cli_under_test", CLI)
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results = []


def check(name, desc, cond, detail=""):
    _results.append(bool(cond))
    print(f"  {PASS if cond else FAIL}  {name:<32} {desc}")
    if not cond and detail:
        print(f"        → {detail}")


def section(title):
    print(f"\n{title}\n" + "─" * 64)


class Args:
    """Stand-in for the argparse namespace."""
    def __init__(self, **kw):
        self.provider = kw.get("provider")
        self.model = kw.get("model")
        self.scope = kw.get("scope", "user")
        self.hook = kw.get("hook", False)
        self.hook_mode = kw.get("hook_mode", "block")
        self.min_lines = kw.get("min_lines", 350)
        self.force = kw.get("force", False)


KEY_VARS = ["OLLAMA_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
            "GROQ_API_KEY", "TOKENSAVE_API_KEY"]


def clear_keys():
    for v in KEY_VARS:
        os.environ.pop(v, None)


# ---------------------------------------------------------------------------

section("Provider detection")

clear_keys()
check("detect-none", "no keys means nothing is detected", C._detect_providers() == [])

os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
check("detect-one", "a single key is found",
      C._detect_providers() == ["openrouter"], C._detect_providers())

os.environ["GROQ_API_KEY"] = "gsk-test"
check("detect-many", "several keys are all reported",
      set(C._detect_providers()) == {"openrouter", "groq"}, C._detect_providers())
clear_keys()

check("local-has-no-key-var", "the local preset declares no key variable",
      C.PROVIDERS["local"][0] == "")
check("every-provider-has-url", "each preset carries a where-to-get-it URL",
      all(p[1] for p in C.PROVIDERS.values()))
check("every-provider-has-model", "each preset names a default model",
      all(p[2] for p in C.PROVIDERS.values()))


# ---------------------------------------------------------------------------

section("Onboarding (no provider given)")


class Captured:
    """Collects stdout so the guidance text can be asserted on."""
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def run_init(**kw):
    cap = Captured()
    real, sys.stdout = sys.stdout, cap
    try:
        rc = C.cmd_init(Args(**kw))
    finally:
        sys.stdout = real
    return rc, cap.text


clear_keys()
rc, out = run_init()
check("no-keys-exits-nonzero", "a user with no keys is not left half-configured",
      rc == 1)
check("no-keys-explains", "the output says the user supplies the model",
      "YOUR choosing" in out or "your choosing" in out.lower())
check("no-keys-says-nothing-auto", "it is explicit that nothing auto-connects",
      "automatically" in out)
for name in ("openrouter", "deepseek", "groq", "local"):
    check(f"no-keys-lists-{name}", f"{name} is offered as an option", name in out)
check("no-keys-shows-env-var", "the exact variable to export is shown",
      "export OPENROUTER_API_KEY" in out)
check("no-keys-shows-next-command", "the next command is spelled out",
      "--provider" in out)
check("local-needs-nothing", "the no-key option is presented",
      "nothing to set" in out or "no key" in out)

os.environ["DEEPSEEK_API_KEY"] = "sk-ds-test"
rc, out = run_init(scope="local")
check("one-key-autodetected", "a lone key is used without being asked for",
      "found a key for deepseek" in out, out[:160])

os.environ["GROQ_API_KEY"] = "gsk-test"
rc, out = run_init()
check("many-keys-asks", "several keys means the user picks, not the tool",
      rc == 1 and "Pick one" in out, out[:160])
clear_keys()


# ---------------------------------------------------------------------------

section("MCP registration command")

# The registration command is assembled by hand; a wrong flag shape silently
# broke `--provider local` once already, so assert its exact form.
import subprocess

recorded = {}


def fake_run(cmd, **kw):
    recorded["cmd"] = cmd

    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


real_run, subprocess.run = subprocess.run, fake_run
real_which, C.shutil.which = C.shutil.which, lambda n: "/usr/bin/claude"
C.subprocess.run = fake_run
try:
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    run_init(provider="openrouter", scope="user")
finally:
    subprocess.run = real_run
    C.subprocess.run = real_run
    C.shutil.which = real_which
    clear_keys()

cmd = recorded.get("cmd", [])
check("cmd-built", "a registration command was assembled", bool(cmd))
env_args = [a for a in cmd if a.startswith("--env=")]
check("env-is-single-arg", "env vars are one --env=K=V argument each",
      len(env_args) >= 2,
      f"got {env_args!r}")
check("env-not-split", "the short -e form is not used (it eats the server name)",
      "-e" not in cmd, cmd)
check("provider-recorded", "the chosen provider is written into the config",
      any(a == "--env=TOKENSAVE_PROVIDER=openrouter" for a in cmd), env_args)
check("key-recorded", "the key is passed to the server",
      any(a.startswith("--env=TOKENSAVE_API_KEY=") for a in cmd), env_args)
if cmd:
    name_idx = cmd.index("token-save") if "token-save" in cmd else -1
    check("name-after-flags", "the server name follows the flags",
          name_idx > 0 and cmd[name_idx + 1] == "--", cmd[name_idx:name_idx + 2])


# ---------------------------------------------------------------------------
#  Repeat detection
# ---------------------------------------------------------------------------

section("Repeat detection")


def repeats_out(rows):
    cap = Captured()
    real, sys.stdout = sys.stdout, cap
    try:
        C._report_repeats(rows)
    finally:
        sys.stdout = real
    return cap.text


def row(path, qhash, worker_in=5000):
    return {"paths": [path], "q": qhash, "worker_in": worker_in,
            "direct_tokens": 7000, "context_tokens": 200, "lines": 600}


# Under the threshold: two calls on one file is normal, not a finding.
out = repeats_out([row("/a.py", "h1"), row("/a.py", "h2")])
check("under-threshold-quiet", "two calls on a file is not reported", out == "", out)

# The wasteful case: same file, same question, several times.
out = repeats_out([row("/a.py", "same")] * 4)
check("same-question-flagged", "repeats of one question are reported",
      "/a.py" in out and "4×" in out, out)
check("same-question-counted", "the identical ones are counted",
      "3 with an identical question" in out, out)
check("same-question-advises", "and the advice says what to do",
      "same question" in out.lower(), out)

# The legitimate case: same file, genuinely different questions.
out = repeats_out([row("/b.py", f"q{i}") for i in range(4)])
check("different-questions-softer", "different questions are not called waste",
      "all different questions" in out, out)
check("different-questions-advises", "but batching is still suggested",
      "one call" in out.lower(), out)

# Cost is attributed to the file, so the reader knows what it is worth fixing.
out = repeats_out([row("/c.py", "same", worker_in=10_000)] * 3)
check("reports-spend", "worker tokens spent on the file are shown",
      "30K" in out or "30.0K" in out, out)

# A ledger written before paths existed must explain itself, not stay silent.
out = repeats_out([{"direct_tokens": 100, "context_tokens": 10}] * 5)
check("legacy-ledger-explained", "an old ledger says why it cannot report",
      "before v0.3" in out, out)

# Rows with paths but no question hash must not crash or miscount.
out = repeats_out([{"paths": ["/d.py"], "worker_in": 100}] * 3)
check("missing-hash-safe", "rows without a question hash still report",
      "/d.py" in out and "identical" not in out, out)

check("no-rows-no-output", "an empty ledger prints nothing", repeats_out([]) == "")


# ---------------------------------------------------------------------------

print("\n" + "═" * 64)
passed, total = sum(_results), len(_results)
colour = "\033[32m" if passed == total else "\033[31m"
print(f"Total: {colour}{passed} passed\033[0m, "
      f"\033[31m{total - passed} failed\033[0m, {total} total")
sys.exit(0 if passed == total else 1)
