"""Cancanement output contract shared by streamed and robot sounds."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class QuackSink(Protocol):
    """Play a tagged sound without exposing where the audio is rendered."""

    async def quack(self, tag: str, hold_s: float = 0.0) -> None:
        """Play tag, holding it for hold_s seconds when the tag supports holds."""
        ...

    async def cancel(self) -> None:
        """Immediately cancel the current cancanement."""
        ...
