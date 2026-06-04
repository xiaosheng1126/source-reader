# /read

Read a source through the local source-reader.

Usage:

```bash
python3 scripts/source_reader.py read "$ARGUMENTS" --remote --read-depth preview --format md
```

If the local service is unavailable:

```bash
python3 scripts/source_reader.py read "$ARGUMENTS" --read-depth preview --format md
```

If login is required:

```bash
python3 scripts/source_reader.py read "$ARGUMENTS" --remote --action login_with_browser --format md
```

Only summarize or answer based on source-reader output. Do not create knowledge-base notes from this command.
