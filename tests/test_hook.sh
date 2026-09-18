#!/bin/bash
# shunt hook evals — verifies the Read hook's routing decisions.
HOOK="python3 $(cd "$(dirname "$0")/.." && pwd)/hooks/check_file_size.py"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

BIG="$TMP/big.py";   seq 1 500 | sed 's/^/# line /' > "$BIG"
SMALL="$TMP/small.py"; seq 1 10 | sed 's/^/# line /' > "$SMALL"
BIN="$TMP/blob.bin"; head -c 4000 /dev/urandom > "$BIN"

check(){ # name  expected  json
  local name="$1" want="$2" json="$3"
  local got; got=$(printf '%s' "$json" | $HOOK | jq -r '.decision' 2>/dev/null)
  if [ "$got" = "$want" ]; then
    printf '  \033[32mPASS\033[0m  %-26s %s\n' "$name" "$4"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m  %-26s want=%s got=%s\n' "$name" "$want" "$got"; fail=$((fail+1))
  fi
}

echo "Read hook routing"
echo "────────────────────────────────────────────────────────────────"
check large-file-blocked  block "{\"tool_input\":{\"file_path\":\"$BIG\"}}"                 "500-line file is blocked"
check small-file-allowed  allow "{\"tool_input\":{\"file_path\":\"$SMALL\"}}"               "under threshold, not worth delegating"
check offset-allowed      allow "{\"tool_input\":{\"file_path\":\"$BIG\",\"offset\":10}}"   "targeted read (offset) passes"
check limit-allowed       allow "{\"tool_input\":{\"file_path\":\"$BIG\",\"limit\":50}}"    "targeted read (limit) passes"
check both-allowed        allow "{\"tool_input\":{\"file_path\":\"$BIG\",\"offset\":5,\"limit\":20}}" "offset+limit passes"
check missing-file        allow "{\"tool_input\":{\"file_path\":\"$TMP/ghost.py\"}}"        "let Read report the error"
check empty-path          allow '{"tool_input":{"file_path":""}}'                           "empty path passes"
check no-field            allow '{"tool_input":{}}'                                         "no file_path passes"
check directory           allow "{\"tool_input\":{\"file_path\":\"$TMP\"}}"                 "a directory is not a file read"
check binary-allowed      allow "{\"tool_input\":{\"file_path\":\"$BIN\"}}"                 "binary is useless to delegate"
check malformed-json      allow 'not json at all'                                           "malformed input never blocks"

# The block reason must be actionable: name the tool and the escape hatch.
reason=$(printf '{"tool_input":{"file_path":"%s"}}' "$BIG" | $HOOK | jq -r '.reason')
for needle in bulk_read offset "$BIG"; do
  case "$reason" in
    *"$needle"*) printf '  \033[32mPASS\033[0m  %-26s reason names %s\n' "reason-names-$needle" "$needle"; pass=$((pass+1));;
    *)           printf '  \033[31mFAIL\033[0m  %-26s missing %s\n' "reason-names-$needle" "$needle"; fail=$((fail+1));;
  esac
done

# Threshold is configurable.
got=$(TOKENSAVE_MIN_LINES=1000 sh -c "printf '{\"tool_input\":{\"file_path\":\"$BIG\"}}' | $HOOK" | jq -r '.decision')
[ "$got" = allow ] && { printf '  \033[32mPASS\033[0m  %-26s raising the threshold allows it\n' threshold-raise; pass=$((pass+1)); } \
                   || { printf '  \033[31mFAIL\033[0m  %-26s got=%s\n' threshold-raise "$got"; fail=$((fail+1)); }
got=$(TOKENSAVE_MIN_LINES=abc sh -c "printf '{\"tool_input\":{\"file_path\":\"$BIG\"}}' | $HOOK" | jq -r '.decision')
[ "$got" = block ] && { printf '  \033[32mPASS\033[0m  %-26s a garbage threshold falls back to 350\n' threshold-garbage; pass=$((pass+1)); } \
                   || { printf '  \033[31mFAIL\033[0m  %-26s got=%s\n' threshold-garbage "$got"; fail=$((fail+1)); }

# Warn mode: the read goes through, but the agent is told what it cost.
got=$(TOKENSAVE_HOOK_MODE=warn sh -c "printf '{\"tool_input\":{\"file_path\":\"$BIG\"}}' | $HOOK" | jq -r '.decision')
[ "$got" = allow ] && { printf '  \033[32mPASS\033[0m  %-26s warn mode lets the read through\n' warn-mode-allows; pass=$((pass+1)); } \
                   || { printf '  \033[31mFAIL\033[0m  %-26s got=%s\n' warn-mode-allows "$got"; fail=$((fail+1)); }

reason=$(TOKENSAVE_HOOK_MODE=warn sh -c "printf '{\"tool_input\":{\"file_path\":\"$BIG\"}}' | $HOOK" | jq -r '.reason')
case "$reason" in
  *bulk_read*) printf '  \033[32mPASS\033[0m  %-26s warn still names the alternative\n' warn-mode-explains; pass=$((pass+1));;
  *)           printf '  \033[31mFAIL\033[0m  %-26s reason=%s\n' warn-mode-explains "$reason"; fail=$((fail+1));;
esac

got=$(TOKENSAVE_HOOK_MODE=nonsense sh -c "printf '{\"tool_input\":{\"file_path\":\"$BIG\"}}' | $HOOK" | jq -r '.decision')
[ "$got" = block ] && { printf '  \033[32mPASS\033[0m  %-26s an unknown mode falls back to block\n' warn-mode-garbage; pass=$((pass+1)); } \
                   || { printf '  \033[31mFAIL\033[0m  %-26s got=%s\n' warn-mode-garbage "$got"; fail=$((fail+1)); }

# The whole reason this hook is Python: it must work on a machine without jq.
got=$(env PATH=/usr/bin:/bin sh -c "command -v jq >/dev/null && echo has-jq || echo no-jq")
out=$(printf '{"tool_input":{"file_path":"%s"}}' "$BIG" | $HOOK)
case "$out" in
  *'"block"'*) printf '  \033[32mPASS\033[0m  %-26s no external tools needed\n' no-jq-dependency; pass=$((pass+1));;
  *)           printf '  \033[31mFAIL\033[0m  %-26s out=%s\n' no-jq-dependency "$out"; fail=$((fail+1));;
esac

# A path containing a quote must still produce valid JSON.
WEIRD="$TMP/we\"ird.py"; seq 1 500 > "$WEIRD" 2>/dev/null
out=$(printf '{"tool_input":{"file_path":%s}}' "$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$WEIRD")" | $HOOK)
if printf '%s' "$out" | python3 -c "import json,sys;json.load(sys.stdin)" 2>/dev/null; then
  printf '  \033[32mPASS\033[0m  %-26s a quoted filename yields valid JSON\n' weird-path-json; pass=$((pass+1))
else
  printf '  \033[31mFAIL\033[0m  %-26s out=%s\n' weird-path-json "$out"; fail=$((fail+1))
fi

echo
echo "════════════════════════════════════════════════════════════════"
printf 'Total: \033[32m%d passed\033[0m, \033[31m%d failed\033[0m, %d total\n' "$pass" "$fail" "$((pass+fail))"
[ "$fail" -eq 0 ]
