"""Outbound ports (interfaces)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> str: ...


@runtime_checkable
class EmbeddingPort(Protocol):
    model_id: str
    dim: int

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...

    def available(self) -> bool: ...


@runtime_checkable
class LLMPort(Protocol):
    def complete_json(self, system: str, user: str) -> dict: ...

    def available(self) -> bool: ...
