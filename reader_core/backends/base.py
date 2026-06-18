from __future__ import annotations

import dataclasses
from typing import Callable, Protocol

from reader_core.models import ReaderOutput


@dataclasses.dataclass
class BackendStatus:
    id: str
    available: bool
    reason: str = ""
    setup_action_id: str = ""
    quality: str = "optional"


@dataclasses.dataclass
class ReadContext:
    source: str
    mode: str
    read_depth: str
    max_chars: int
    browser_profile: str = ".source-reader/profiles/default"
    headless: bool = False
    interactive_login: bool = False
    login_timeout_ms: int = 180000
    allow_external: bool = False


class ReaderBackend(Protocol):
    id: str
    priority: int

    def can_handle(self, source: str, mode: str) -> bool:
        ...

    def status(self) -> BackendStatus:
        ...

    def read(self, context: ReadContext) -> ReaderOutput:
        ...


@dataclasses.dataclass
class FunctionBackend:
    id: str
    priority: int
    predicate: Callable[[str, str], bool]
    reader: Callable[[ReadContext], ReaderOutput]
    status_reader: Callable[[], BackendStatus] | None = None

    def can_handle(self, source: str, mode: str) -> bool:
        return self.predicate(source, mode)

    def status(self) -> BackendStatus:
        if self.status_reader:
            return self.status_reader()
        return BackendStatus(id=self.id, available=True, quality="builtin")

    def read(self, context: ReadContext) -> ReaderOutput:
        return self.reader(context)
