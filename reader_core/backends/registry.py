from __future__ import annotations

from reader_core.backends.base import BackendStatus, ReaderBackend


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: list[ReaderBackend] = []

    def register(self, backend: ReaderBackend) -> None:
        self._backends.append(backend)

    def candidates(self, source: str, mode: str) -> list[ReaderBackend]:
        matches = [backend for backend in self._backends if backend.can_handle(source, mode)]
        return sorted(matches, key=lambda backend: backend.priority)

    def statuses(self) -> list[BackendStatus]:
        return [backend.status() for backend in sorted(self._backends, key=lambda item: item.priority)]
