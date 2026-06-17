from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass
class ReaderOutput:
    input_type: str
    source_type: str
    title: str
    run_id: str = ""
    url: str = ""
    local_path: str = ""
    author: str = ""
    published_at: str = ""
    fetched_at: str = dataclasses.field(
        default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds")
    )
    reader: str = "scripts/source_reader.py"
    read_quality: str = "basic"
    confidence: int = 0
    strategy: str = ""
    token_policy: str = ""
    read_depth: str = "standard"
    content: str = ""
    preview: dict[str, object] = dataclasses.field(default_factory=dict)
    actions: list[dict[str, object]] = dataclasses.field(default_factory=list)
    next_actions: list[dict[str, object]] = dataclasses.field(default_factory=list)
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    assets: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)
