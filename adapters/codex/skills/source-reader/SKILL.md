# Source Reader

Use this skill when the user asks to read a URL, GitHub repository, PDF, video page, discussion thread, or local document with source-reader.

## Workflow

1. Prefer the local service entry:

```bash
python3 scripts/source_reader.py remote-read <source> --read-depth preview --format md
```

2. If the service is unavailable, use the direct CLI:

```bash
python3 scripts/source_reader.py <source> --mode auto --browser-profile .source-reader/profiles/default --read-depth preview --format md
```

3. If the output says login or JS rendering blocked the read, run the reader action:

```bash
python3 scripts/source_reader.py action login_with_browser --source <source> --format md
```

4. Use reader actions for follow-up work:

```bash
python3 scripts/source_reader.py action continue_deep_read --source <source> --format md
python3 scripts/source_reader.py action extract_outline --source <source> --format md
python3 scripts/source_reader.py action extract_code --source <source> --format md
```

## Boundaries

- source-reader only reads and structures source content.
- Do not treat it as a knowledge-base workflow.
- Do not write raw/wiki or modify user knowledge notes from this skill.
- For large sources, start with `--read-depth preview` and ask before deep reading.
