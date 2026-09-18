# Contributing

Bug reports and patches are welcome.

## Running the tests

```bash
pip install -e .
python tests/test_server.py    # 108 server tests — no API calls
python tests/test_cli.py       # 34 CLI tests
bash tests/test_hook.sh        # 21 hook routing tests (needs jq)
```

The transport is stubbed, so the suite is free to run and safe in CI.

## Ground rules

- **A new behaviour needs a test.** The suite exists because several real
  defects (a dangling-symlink write, a corpus that ate all memory, a hook that
  emitted invalid JSON on odd filenames) were found by review and would
  otherwise have come back.
- **Don't pin a model roster.** Any model id the provider serves is valid; a
  hardcoded list goes stale the moment a provider ships something new.
- **Savings must stay measured.** The footer reports the provider's own `usage`
  numbers. Don't replace them with estimates.
- **The hook fails open.** If anything is uncertain — no jq, malformed input,
  unreadable file — it allows the read. Blocking by accident is worse than
  missing a saving.
