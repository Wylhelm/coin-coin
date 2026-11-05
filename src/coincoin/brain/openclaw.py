"""OpenAI-compatible HTTP client for the OpenClaw gateway."""

import asyncio
import logging
import random
from collections.abc import Mapping
from typing import Any, Literal

import httpx

from coincoin.brain.base import Brain
from coincoin.config import Settings

_LOGGER = logging.getLogger(__name__)
_USER = "coin-coin-voice"
_START_PROMPT = (
    "[Coin-Coin se réveille] Donne en 5 lignes max ce que je dois savoir pour "
    "parler à l'utilisateur maintenant (souvenirs utiles, préférences)."
)


class OpenClawError(RuntimeError):
    """A safe, non-secret OpenClaw request failure."""


class OpenClawAuthError(OpenClawError):
    """The gateway rejected its configured credentials or authorization."""


class OpenClawArchivedSessionError(OpenClawError):
    """The stable OpenClaw session is archived and must be restored."""


class OpenClawUnavailableError(OpenClawError):
    """The gateway could not complete a request."""


FailureKind = Literal["auth", "archived", "unavailable"]


def failure_kind(error: BaseException) -> FailureKind:
    """Classify a brain failure without exposing its remote details."""
    if isinstance(error, OpenClawAuthError):
        return "auth"
    if isinstance(error, OpenClawArchivedSessionError):
        return "archived"
    return "unavailable"


SPOKEN_FAILURES: dict[FailureKind, str] = {
    "auth": "Ma mémoire est fermée à clé.",
    "archived": "Ma mémoire est archivée ; il faut la restaurer.",
    "unavailable": "Ma mémoire ne répond pas pour le moment.",
}


class OpenClawBrain:
    """Delegate memory and long-form reasoning to one stable OpenClaw agent."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self.last_failure: FailureKind | None = None

    async def close(self) -> None:
        """Close the HTTP client when this brain created it."""
        if self._owns_client:
            await self._client.aclose()

    async def session_start(self) -> str:
        """Load brief memory context; a slow or unavailable brain is non-blocking."""
        self.last_failure = None
        try:
            async with asyncio.timeout(3):
                return await self._complete(_START_PROMPT, timeout_s=3)
        except (OpenClawError, TimeoutError) as exc:
            self.last_failure = failure_kind(exc)
            _LOGGER.error("OpenClaw session start failed: %s", type(exc).__name__)
            return ""

    async def ask(self, message: str) -> str:
        """Ask the agent, returning a safe spoken explanation on gateway failures."""
        self.last_failure = None
        try:
            async with asyncio.timeout(8):
                return await self._complete(message, timeout_s=8)
        except (OpenClawError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                _LOGGER.error("OpenClaw ask exceeded its 8-second deadline")
            self.last_failure = failure_kind(exc)
            return SPOKEN_FAILURES[self.last_failure]

    async def remember(self, fact: str) -> None:
        """Store a fact in the agent's persistent conversation."""
        async with asyncio.timeout(8):
            await self._complete(f"[À mémoriser] {fact}", timeout_s=8)

    async def session_end(self, summary: str) -> None:
        """Persist a locally constructed conversation summary."""
        async with asyncio.timeout(15):
            await self._complete(
                f"[Fin de conversation] Résumé : {summary}. Mémorise ce qui mérite de l'être.",
                timeout_s=15,
            )

    async def _complete(self, prompt: str, *, timeout_s: float) -> str:
        token = self._settings.openclaw_token.get_secret_value()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "model": f"openclaw/{self._settings.openclaw_agent}",
            "user": _USER,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        url = f"{self._settings.openclaw_url.rstrip('/')}/v1/chat/completions"
        delay_s = 0.25
        for attempt in range(3):
            try:
                response = await self._client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=httpx.Timeout(timeout_s),
                )
            except httpx.RequestError as exc:
                if attempt < 2:
                    _LOGGER.warning("OpenClaw network request failed: %s", type(exc).__name__)
                    await asyncio.sleep(random.uniform(delay_s * 0.8, delay_s * 1.2))
                    delay_s = min(1.0, delay_s * 2)
                    continue
                _LOGGER.error("OpenClaw network request exhausted retries: %s", type(exc).__name__)
                raise OpenClawUnavailableError("OpenClaw network request failed") from exc

            if response.status_code in {401, 403}:
                _LOGGER.error("OpenClaw authorization rejected (HTTP %d)", response.status_code)
                raise OpenClawAuthError(f"OpenClaw authorization failed ({response.status_code})")
            if response.status_code == 409:
                _LOGGER.error("OpenClaw session is archived (HTTP 409)")
                raise OpenClawArchivedSessionError("OpenClaw session is archived (409)")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    _LOGGER.warning(
                        "OpenClaw returned retryable HTTP status %d", response.status_code
                    )
                    await asyncio.sleep(random.uniform(delay_s * 0.8, delay_s * 1.2))
                    delay_s = min(1.0, delay_s * 2)
                    continue
                _LOGGER.error("OpenClaw request exhausted retries (HTTP %d)", response.status_code)
                raise OpenClawUnavailableError(
                    f"OpenClaw returned HTTP status {response.status_code}"
                )
            if response.is_error:
                _LOGGER.error("OpenClaw rejected request (HTTP %d)", response.status_code)
                raise OpenClawError(f"OpenClaw returned HTTP status {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                _LOGGER.error("OpenClaw returned invalid JSON")
                raise OpenClawError("OpenClaw returned invalid JSON") from exc
            return _response_content(body)
        raise AssertionError("OpenClaw retry loop exited unexpectedly")


def _response_content(body: Any) -> str:
    if not isinstance(body, Mapping):
        _LOGGER.error("OpenClaw response is not an object")
        raise OpenClawError("OpenClaw response is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        _LOGGER.error("OpenClaw response has no choices")
        raise OpenClawError("OpenClaw response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        _LOGGER.error("OpenClaw response has no text content")
        raise OpenClawError("OpenClaw response has no text content")
    content = message["content"].strip()
    if not content:
        _LOGGER.error("OpenClaw response text is empty")
        raise OpenClawError("OpenClaw response text is empty")
    return content


def configured_brain(settings: Settings) -> Brain:
    """Build the remote brain only when a gateway token is configured."""
    if not settings.openclaw_token.get_secret_value():
        from coincoin.brain.base import NullBrain

        return NullBrain()
    return OpenClawBrain(settings)
