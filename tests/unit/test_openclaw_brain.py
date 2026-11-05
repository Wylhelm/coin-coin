import logging
import time

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from coincoin.brain.base import NullBrain
from coincoin.brain.openclaw import (
    OpenClawArchivedSessionError,
    OpenClawAuthError,
    OpenClawBrain,
    OpenClawError,
    OpenClawUnavailableError,
    configured_brain,
)
from coincoin.brain.summary import build_session_summary
from coincoin.config import Settings
from tests.fakes.fake_openclaw import FakeOpenClaw

_TOKEN = "test-openclaw-token-not-for-production"


async def _brain(fake: FakeOpenClaw, token: str = _TOKEN) -> tuple[OpenClawBrain, TestClient]:
    client = TestClient(TestServer(fake.app))
    await client.start_server()
    settings = Settings(
        openclaw_url=str(client.make_url("")).rstrip("/"),
        openclaw_token=SecretStr(token),
        openclaw_agent="coin-coin",
    )
    return OpenClawBrain(settings), client


async def test_chat_request_uses_documented_agent_and_stable_user() -> None:
    fake = FakeOpenClaw(expected_token=_TOKEN)
    brain, server = await _brain(fake)
    try:
        assert await brain.ask("Quel est mon prénom ?") == "Souvenir de test."
        request = fake.requests[0]
        assert request["body"] == {
            "model": "openclaw/coin-coin",
            "user": "coin-coin-voice",
            "messages": [{"role": "user", "content": "Quel est mon prénom ?"}],
            "stream": False,
        }
    finally:
        await brain.close()
        await server.close()


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, OpenClawAuthError), (403, OpenClawAuthError), (409, OpenClawArchivedSessionError)],
)
async def test_auth_and_archived_session_errors_are_typed(status, error) -> None:
    brain, server = await _brain(FakeOpenClaw(status=status))
    try:
        with pytest.raises(error):
            await brain.remember("un fait")
    finally:
        await brain.close()
        await server.close()


async def test_retryable_gateway_error_is_explicit_after_backoff() -> None:
    brain, server = await _brain(FakeOpenClaw(status=500))
    try:
        with pytest.raises(OpenClawUnavailableError, match="HTTP status 500"):
            await brain.remember("un fait")
    finally:
        await brain.close()
        await server.close()


async def test_retryable_gateway_error_recovers_on_following_attempt() -> None:
    fake = FakeOpenClaw(statuses=[500, 429, 200])
    brain, server = await _brain(fake)
    try:
        assert await brain.ask("ping") == "Souvenir de test."
        assert brain.last_failure is None
        assert len(fake.requests) == 3
    finally:
        await brain.close()
        await server.close()


async def test_start_timeout_returns_empty_context_without_blocking() -> None:
    brain, server = await _brain(FakeOpenClaw(delay_s=3.5))
    try:
        started = time.monotonic()
        assert await brain.session_start() == ""
        assert time.monotonic() - started < 3.4
    finally:
        await brain.close()
        await server.close()


async def test_start_unavailable_gateway_returns_empty_context() -> None:
    brain, server = await _brain(FakeOpenClaw(status=500))
    try:
        assert await brain.session_start() == ""
    finally:
        await brain.close()
        await server.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "Ma mémoire est fermée à clé."),
        (409, "Ma mémoire est archivée ; il faut la restaurer."),
    ],
)
async def test_ask_returns_explicit_auth_and_archived_session_messages(status, expected) -> None:
    brain, server = await _brain(FakeOpenClaw(status=status))
    try:
        assert await brain.ask("question") == expected
    finally:
        await brain.close()
        await server.close()


async def test_session_end_and_remember_use_their_local_prompt_templates() -> None:
    fake = FakeOpenClaw()
    brain, server = await _brain(fake)
    try:
        await brain.remember("un fait")
        await brain.session_end("résumé local")
        assert fake.requests[0]["body"]["messages"][0]["content"] == "[À mémoriser] un fait"
        assert fake.requests[1]["body"]["messages"][0]["content"] == (
            "[Fin de conversation] Résumé : résumé local. Mémorise ce qui mérite de l'être."
        )
    finally:
        await brain.close()
        await server.close()


async def test_bad_http_status_and_malformed_response_are_explicit() -> None:
    brain, server = await _brain(FakeOpenClaw(status=400))
    try:
        with pytest.raises(OpenClawError, match="HTTP status 400"):
            await brain.remember("un fait")
    finally:
        await brain.close()
        await server.close()

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"choices": []}))
    brain = OpenClawBrain(Settings(), client=httpx.AsyncClient(transport=transport))
    try:
        with pytest.raises(OpenClawError, match="no choices"):
            await brain.remember("un fait")
    finally:
        await brain.close()


async def test_network_failure_retries_without_logging_url_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def fail(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("loopback fake unavailable")

    transport = httpx.MockTransport(fail)
    brain = OpenClawBrain(
        Settings(
            openclaw_url="http://loopback.invalid",
            openclaw_token=SecretStr(_TOKEN),
        ),
        client=httpx.AsyncClient(transport=transport),
    )
    try:
        assert await brain.ask("question") == "Ma mémoire ne répond pas pour le moment."
        assert attempts == 3
        assert "loopback.invalid" not in caplog.text
        assert _TOKEN not in caplog.text
    finally:
        await brain.close()


def test_unconfigured_brain_is_network_free() -> None:
    assert isinstance(configured_brain(Settings()), NullBrain)


async def test_access_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeOpenClaw(status=401)
    brain, server = await _brain(fake)
    caplog.set_level(logging.DEBUG)
    try:
        assert await brain.ask("Souviens-toi") == "Ma mémoire est fermée à clé."
        assert _TOKEN not in caplog.text
        assert all(_TOKEN not in repr(request) for request in fake.requests)
    finally:
        await brain.close()
        await server.close()


def test_summary_is_constructed_locally_and_bounded() -> None:
    summary = build_session_summary(
        ["Bonjour user.", "Tu préfères le français."],
        ["look gauche", "move refusé"],
        max_chars=45,
    )
    assert len(summary) <= 45
    assert summary.endswith("Action: move refusé")
    assert build_session_summary([], []) == ""
    with pytest.raises(ValueError, match="positive"):
        build_session_summary([], [], max_chars=0)
