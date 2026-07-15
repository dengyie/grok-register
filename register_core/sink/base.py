from __future__ import annotations

from typing import Protocol, runtime_checkable

from register_core.contracts import RegisterResult


@runtime_checkable
class ResultSink(Protocol):
    name: str

    def write(self, result: RegisterResult) -> None:
        ...
