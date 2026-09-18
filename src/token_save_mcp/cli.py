#!/usr/bin/env python3
"""
token-save-mcp CLI — install, diagnose, uninstall.

The point of `init` is that nobody should hand-edit JSON to try this. One
command registers the MCP server, optionally installs the enforcement hook,
and tells you what to do next.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _find_hook() -> pathlib.Path | None:
    """Locate the hook script.

    Installed wheels carry it inside the package; a source checkout keeps it at
    the repo root. Check both rather than assuming one layout — getting this
    wrong means `install-hook` fails for everyone who used pip.
    """
    for candidate in (
        HERE / "hooks" / "check-file-size",            # installed package
        HERE.parent.parent / "hooks" / "check-file-size",  # source checkout
    ):
        if candidate.exists():
            return candidate
    return None


HOOK_SRC = _find_hook()

PROVIDERS = {
    "ollama": ("OLLAMA_API_KEY", "https://ollama.com/settings/keys", "glm-5.3-flash"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/keys",
                   "deepseek/deepseek-chat"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys",
                 "deepseek-chat"),
    "groq": ("GROQ_API_KEY", "https://console.groq.com/keys",
             "llama-3.3-70b-versatile"),
    "local": ("", "http://localhost:11434 (no key needed)", "qwen2.5-coder:7b"),
}

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
)


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}!{RESET} {msg}")


def bad(msg):
    print(f"  {RED}✗{RESET} {msg}")


def _claude_settings_path() -> pathlib.Path:
    return pathlib.Path.home() / ".claude" / "settings.json"


def _server_command() -> list[str]:
    """How to launch the server. Prefer the installed console script."""
    exe = shutil.which("token-save-mcp-server")
    if exe:
        return [exe]
    return [sys.executable, "-m", "token_save_mcp.server"]


# ---------------------------------------------------------------------------
#  init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    provider = args.provider
    key_var, key_url, default_model = PROVIDERS[provider]

    print(f"\ntoken-save-mcp — setup ({provider})\n")

    # 1. Key
    key = os.environ.get("TOKENSAVE_API_KEY") or (
        os.environ.get(key_var) if key_var else ""
    )
    if key:
        ok(f"API key found in ${key_var or 'TOKENSAVE_API_KEY'}")
    elif provider == "local":
        ok("local provider — no API key needed")
    else:
        bad(f"no API key in ${key_var}")
        print(f"    Get one at: {key_url}")
        print(f"    Then: export {key_var}=...  (add it to your shell profile)")
        if not args.force:
            print("\n  Re-run once the key is set, or pass --force to register anyway.\n")
            return 1
        warn("--force given: registering without a verified key")

    env = {}
    if provider != "ollama":
        env["TOKENSAVE_PROVIDER"] = provider
    if key:
        env["TOKENSAVE_API_KEY"] = key
    if args.model:
        env["TOKENSAVE_MODEL"] = args.model

    # 2. Register the MCP server
    cmd = ["claude", "mcp", "add", "--scope", args.scope, "--transport", "stdio"]
    for k, v in env.items():
        cmd += ["--env", f"{k}={v}"]
    cmd += ["token-save", "--"] + _server_command()

    if shutil.which("claude"):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            ok(f"registered MCP server 'token-save' ({args.scope} scope)")
        elif "already exists" in (res.stderr + res.stdout):
            ok("MCP server 'token-save' already registered")
        else:
            bad("could not register the MCP server")
            print((res.stderr or res.stdout).strip()[:400])
            return 1
    else:
        warn("`claude` CLI not found — add this to your MCP config by hand:")
        print(json.dumps({"mcpServers": {"token-save": {
            "type": "stdio",
            "command": _server_command()[0],
            "args": _server_command()[1:],
            "env": env or {},
        }}}, indent=2))

    # 3. The hook — opt-in, because blocking Read is an intrusive default.
    if args.hook:
        rc = _install_hook(args.min_lines)
        if rc != 0:
            return rc
    else:
        print(f"\n  {DIM}Hook not installed (enforcement is opt-in).{RESET}")
        print(f"  {DIM}Add it later with: token-save-mcp install-hook{RESET}")

    model = args.model or default_model
    print(f"""
{GREEN}Done.{RESET} Start a new session, then try:

    Ask your agent:  "use bulk_read to summarise <some big file>"

Worker model: {model}
Check config:  token-save-mcp doctor
""")
    return 0


# ---------------------------------------------------------------------------
#  hook install / uninstall
# ---------------------------------------------------------------------------

def _hook_dest() -> pathlib.Path:
    d = pathlib.Path.home() / ".claude" / "token-save"
    d.mkdir(parents=True, exist_ok=True)
    return d / "check-file-size"


def _install_hook(min_lines: int) -> int:
    if HOOK_SRC is None:
        bad("hook script not found in the installed package or the source tree")
        print("    Reinstall with: pip install --force-reinstall token-save-mcp")
        return 1

    dest = _hook_dest()
    shutil.copy2(HOOK_SRC, dest)
    dest.chmod(0o755)
    ok(f"hook installed at {dest}")

    settings = _claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)

    try:
        cfg = json.loads(settings.read_text()) if settings.exists() else {}
    except json.JSONDecodeError:
        bad(f"{settings} is not valid JSON — fix it first, nothing was changed")
        return 1

    if settings.exists():
        backup = settings.with_suffix(f".json.bak-token-save")
        shutil.copy2(settings, backup)
        ok(f"backed up settings to {backup.name}")

    pre = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
    already = any(
        e.get("matcher") == "Read"
        and any("check-file-size" in h.get("command", "") for h in e.get("hooks", []))
        for e in pre
    )
    if already:
        ok("hook already registered in settings.json")
    else:
        pre.append({"matcher": "Read",
                    "hooks": [{"type": "command", "command": str(dest)}]})
        settings.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        ok(f"hook registered for Read (threshold: {min_lines} lines)")

    if min_lines != 350:
        print(f"    Set TOKENSAVE_MIN_LINES={min_lines} in your environment "
              f"to match.")
    return 0


def cmd_install_hook(args) -> int:
    print("\ntoken-save-mcp — installing the enforcement hook\n")
    rc = _install_hook(args.min_lines)
    if rc == 0:
        print(f"\n{GREEN}Done.{RESET} Reads over the threshold will now be "
              f"redirected to bulk_read.\n"
              f"Remove it with: token-save-mcp uninstall-hook\n")
    return rc


def cmd_uninstall_hook(args) -> int:
    print("\ntoken-save-mcp — removing the hook\n")
    settings = _claude_settings_path()
    if not settings.exists():
        warn("no settings.json — nothing to remove")
        return 0
    try:
        cfg = json.loads(settings.read_text())
    except json.JSONDecodeError:
        bad(f"{settings} is not valid JSON — nothing was changed")
        return 1

    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    kept = [
        e for e in pre
        if not (e.get("matcher") == "Read"
                and any("check-file-size" in h.get("command", "")
                        for h in e.get("hooks", [])))
    ]
    if len(kept) == len(pre):
        warn("hook was not registered")
    else:
        cfg["hooks"]["PreToolUse"] = kept
        settings.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        ok("hook removed from settings.json")

    dest = pathlib.Path.home() / ".claude" / "token-save" / "check-file-size"
    if dest.exists():
        dest.unlink()
        ok(f"deleted {dest}")
    print()
    return 0


# ---------------------------------------------------------------------------
#  doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    print("\ntoken-save-mcp — diagnostics\n")
    problems = 0

    # Python + deps
    ok(f"python {sys.version.split()[0]}")
    for mod in ("mcp", "openai"):
        try:
            __import__(mod)
            ok(f"{mod} importable")
        except ImportError:
            bad(f"{mod} NOT installed — pip install token-save-mcp")
            problems += 1

    # Config — import the server module to resolve exactly as it would
    try:
        from token_save_mcp import server as S
        ok(f"provider: {S.PROVIDER} -> {S.BASE_URL}")
        ok(f"worker model: {S.DEFAULT_MODEL}")
        ok(f"api key: {'set' if S.API_KEY else 'MISSING'}")
        ok(f"threshold: {S.MIN_LINES} lines")
    except SystemExit as exc:
        bad(f"configuration error: {exc}")
        problems += 1
        S = None
    except Exception as exc:
        bad(f"cannot load server: {type(exc).__name__}: {exc}")
        problems += 1
        S = None

    # jq — the hook needs it
    if shutil.which("jq"):
        ok("jq present (required by the hook)")
    else:
        warn("jq not found — the hook will pass everything through. "
             "Install it: apt install jq / brew install jq")

    # MCP registration
    if shutil.which("claude"):
        res = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
        if "token-save" in res.stdout:
            line = next((l for l in res.stdout.splitlines() if "token-save" in l), "")
            if "✔" in line or "Connected" in line:
                ok("MCP server registered and connected")
            else:
                warn(f"MCP registered but not connected: {line.strip()[:120]}")
                problems += 1
        else:
            warn("MCP server not registered — run: token-save-mcp init")
    else:
        warn("`claude` CLI not found — cannot check MCP registration")

    # Hook
    settings = _claude_settings_path()
    if settings.exists():
        try:
            cfg = json.loads(settings.read_text())
            pre = cfg.get("hooks", {}).get("PreToolUse", [])
            if any("check-file-size" in h.get("command", "")
                   for e in pre for h in e.get("hooks", [])):
                ok("enforcement hook installed")
            else:
                warn("hook not installed (optional) — token-save-mcp install-hook")
        except json.JSONDecodeError:
            bad(f"{settings} is not valid JSON")
            problems += 1

    # Live probe
    if S is not None and not args.offline:
        print("\n  probing the worker…")
        import asyncio
        try:
            res = asyncio.run(S._call(S.DEFAULT_MODEL, "Reply with: ok",
                                      "Reply with: ok", "low"))
            if res["ok"]:
                ok(f"worker replied in {res['seconds']:.1f}s "
                   f"({res['in_tokens']} in / {res['out_tokens']} out)")
            else:
                bad(f"worker call failed: {res['text'][:200]}")
                problems += 1
        except Exception as exc:
            bad(f"worker call raised: {type(exc).__name__}: {exc}")
            problems += 1

    if problems:
        print(f"\n{RED}{problems} problem(s) found.{RESET}\n")
        return 1
    print(f"\n{GREEN}All checks passed.{RESET}\n")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="token-save-mcp",
        description="Keep big files out of your agent's context.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="register the MCP server (one command setup)")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama",
                   help="worker provider (default: ollama)")
    p.add_argument("--model", help="worker model id (default: provider's preset)")
    p.add_argument("--scope", choices=["user", "project", "local"], default="user")
    p.add_argument("--hook", action="store_true",
                   help="also install the enforcement hook (blocks large Reads)")
    p.add_argument("--min-lines", type=int, default=350)
    p.add_argument("--force", action="store_true",
                   help="register even without a verified API key")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("install-hook", help="install the Read-blocking hook")
    p.add_argument("--min-lines", type=int, default=350)
    p.set_defaults(func=cmd_install_hook)

    p = sub.add_parser("uninstall-hook", help="remove the hook")
    p.set_defaults(func=cmd_uninstall_hook)

    p = sub.add_parser("doctor", help="diagnose the installation")
    p.add_argument("--offline", action="store_true",
                   help="skip the live worker call")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
