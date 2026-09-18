#!/usr/bin/env python3
"""
token-save PreToolUse hook: keep an expensive Read from reaching the model.

Blocks a full-file Read above a line or byte threshold and points the agent at
`bulk_read` instead. Targeted reads pass through, because editing needs exact
text and a summary will not do.

This is Python rather than shell so it has no external dependency: if the
package is installed, this runs. The previous shell version needed `jq`, which
silently disabled the hook on machines that did not have it.

Every uncertain case fails OPEN — the read is allowed. Blocking by accident is
worse than missing a saving.

Environment:
    TOKENSAVE_MIN_LINES       line threshold (default 350)
    TOKENSAVE_HOOK_MAX_BYTES  byte threshold (default 100000)
    TOKENSAVE_HOOK_MODE       "block" (default) or "warn"
"""

import json
import os
import sys


def allow(reason: str = "") -> None:
    out = {"decision": "allow"}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    sys.exit(0)


def _int_env(name: str, default: int) -> int:
    """Read an integer setting, falling back on anything unparseable."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        allow()  # malformed input is not ours to report

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        allow()

    path = tool_input.get("file_path") or ""

    # A targeted read means the agent already knows what it wants, and is
    # usually about to edit. Never block those.
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        allow()

    if not path or not isinstance(path, str):
        allow()

    # Missing, unreadable, or not a regular file: let Read report it itself.
    try:
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            allow()
        size = os.path.getsize(path)
    except OSError:
        allow()

    min_lines = _int_env("TOKENSAVE_MIN_LINES", 350)
    max_bytes = _int_env("TOKENSAVE_HOOK_MAX_BYTES", 100_000)

    # Count lines without holding the file in memory, and bail out early once
    # the threshold is passed — no reason to read a 2 GB file to the end.
    lines = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                if b"\x00" in chunk and lines == 0:
                    allow()  # binary: a summary of it would be useless
                lines += chunk.count(b"\n")
                if lines > min_lines:
                    break
    except OSError:
        allow()

    if lines <= min_lines and size <= max_bytes:
        allow()

    mode = os.environ.get("TOKENSAVE_HOOK_MODE", "block").strip().lower()
    if mode not in ("block", "warn"):
        mode = "block"

    if mode == "warn":
        allow(f"token-save: reading {lines:,} lines into context. bulk_read "
              f"would have delegated this to a worker instead. "
              f"(warn mode — set TOKENSAVE_HOOK_MODE=block to enforce.)")

    # json.dumps escapes the path correctly, however odd the filename is.
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"This file is {lines:,} lines (threshold: {min_lines}). Use the "
            f"bulk_read MCP tool to delegate the read to a worker model instead "
            f"of spending that context: bulk_read(question=\"<what you need to "
            f"know>\", paths=[\"{path}\"]). If you need exact content in order "
            f"to EDIT this file, re-read it with an offset/limit for just the "
            f"section you are changing — that is allowed through."
        ),
    }))


if __name__ == "__main__":
    main()
