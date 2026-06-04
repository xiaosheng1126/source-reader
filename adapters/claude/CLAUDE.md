# Source Reader Claude Adapter

source-reader is the local reading layer for Claude Code. Reach for it whenever you need to read login-walled sites, internal/intranet docs, local files, or paid subscriptions before summarizing, comparing, or answering.

Preferred command (via local service):

```bash
python3 scripts/source_reader.py read <source> --remote --read-depth preview --format md
```

Direct command (when the service is down):

```bash
python3 scripts/source_reader.py read <source> --mode fast --read-depth preview --format md
```

Actions (apply to the read result):

```bash
python3 scripts/source_reader.py read <source> --remote --action continue_deep_read --format md
python3 scripts/source_reader.py read <source> --remote --action extract_outline --format md
python3 scripts/source_reader.py read <source> --remote --action extract_code --format md
python3 scripts/source_reader.py read <source> --remote --action login_with_browser --format md
```

Rules:

- Start with `preview` for unknown or large sources.
- Fast mode auto-upgrades to browser when confidence < 40, login wall, or JS shell — no need to retry by hand.
- source-reader does not write to any knowledge base. It only returns readable source content and operation suggestions.
