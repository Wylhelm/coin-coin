"""Extension contract shared by the independently developed feature sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from coincoin.app import App


class Feature(Protocol):
    """Install one feature's tools, context providers, and lifecycle handlers."""

    async def install(self, app: App) -> None: ...


FEATURES: list[Feature] = []
