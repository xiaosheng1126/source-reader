# Source Reader

Use this skill when the user asks to read a URL, GitHub repository, PDF, video page, discussion thread, login-walled site, internal/intranet doc, or local file. source-reader is the local reading layer for Codex — it covers what cloud readers cannot reach.

## Workflow

1. Prefer the local service entry:

```bash
python3 scripts/source_reader.py read <source> --remote --read-depth preview --format md
```

2. If the service is unavailable, use the direct CLI:

```bash
python3 scripts/source_reader.py read <source> --read-depth preview --format md
```

   Fast mode auto-upgrades to browser when confidence < 40, login wall, or JS shell — no need to retry by hand.

3. If the output still says login is required, run the reader action:

```bash
python3 scripts/source_reader.py read <source> --remote --action login_with_browser --format md
```

4. Follow-up actions on the same source:

```bash
python3 scripts/source_reader.py read <source> --remote --action continue_deep_read --format md
python3 scripts/source_reader.py read <source> --remote --action extract_outline --format md
python3 scripts/source_reader.py read <source> --remote --action extract_code --format md
```

5. Quick environment check when something looks off:

```bash
python3 scripts/source_reader.py status --format md
```

## Boundaries

- source-reader only reads and structures source content.
- Do not treat it as a knowledge-base workflow.
- Do not write raw/wiki or modify user knowledge notes from this skill.
- For large sources, start with `--read-depth preview` and ask before deep reading.
