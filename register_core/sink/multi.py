from __future__ import annotations

from register_core.contracts import RegisterResult
from register_core.sink.base import ResultSink


class MultiSink:
    name = "multi"

    def __init__(self, sinks: list[ResultSink]) -> None:
        self.sinks = list(sinks)

    def write(self, result: RegisterResult) -> None:
        for s in self.sinks:
            s.write(result)
