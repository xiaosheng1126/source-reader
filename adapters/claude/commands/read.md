# /read

Read a source through local source-reader.

Usage:

```bash
python3 scripts/source_reader.py remote-read "$ARGUMENTS" --read-depth preview --format md
```

If the local service is unavailable:

```bash
python3 scripts/source_reader.py "$ARGUMENTS" --mode auto --browser-profile .source-reader/profiles/default --read-depth preview --format md
```

If login is required:

```bash
python3 scripts/source_reader.py action login_with_browser --source "$ARGUMENTS" --format md
```

Only summarize or answer based on source-reader output. Do not create knowledge-base notes from this command.
