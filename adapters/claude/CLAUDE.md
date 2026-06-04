# Source Reader Claude Adapter

Use source-reader for reading external or local sources before summarizing, comparing, or answering questions.

Preferred command:

```bash
python3 scripts/source_reader.py remote-read <source> --read-depth preview --format md
```

Fallback command:

```bash
python3 scripts/source_reader.py <source> --mode auto --browser-profile .source-reader/profiles/default --read-depth preview --format md
```

Rules:

- Start with preview for large or unknown sources.
- Use `continue_deep_read`, `extract_outline`, `extract_code`, or `login_with_browser` actions when needed.
- source-reader is not a knowledge-base writer. It only returns readable source content and operation suggestions.
