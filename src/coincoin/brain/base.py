"""Memory contract with an honest, network-free default implementation."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Brain(Protocol):
    """Provide long-term memory and reasoning for a voice session."""

    async def session_start(self) -> str:
        """Return the memory context to inject at conversation startup."""
        ...

    async def ask(self, message: str) -> str:
        """Answer a request for memory or long-form reasoning."""
        ...

    async def remember(self, fact: str) -> None:
        """Store a fact for future conversations."""
        ...

    async def session_end(self, summary: str) -> None:
        """Store a conversation summary when the session ends."""
        ...


class NullBrain:
    """Offline default: persist nothing and state that memory is unavailable."""

    async def session_start(self) -> str:
        """Return an explicit notice that this brain has no memory."""
        return "Je n'ai pas de memoire : OpenClaw n'est pas configure."

    async def ask(self, message: str) -> str:
        """Explain that no memory or delegated reasoning is available."""
        return "Je n'ai pas de memoire et je ne peux pas consulter OpenClaw."

    async def remember(self, fact: str) -> None:
        """Do not persist facts in the explicitly memory-free default."""

    async def session_end(self, summary: str) -> None:
        """Do not persist summaries in the explicitly memory-free default."""
