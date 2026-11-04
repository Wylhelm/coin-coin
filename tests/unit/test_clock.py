import asyncio
import math

import pytest

from coincoin.clock import Clock, FakeClock, SystemClock


async def test_fake_clock_releases_deadlines_and_ties_in_order() -> None:
    clock = FakeClock(start=10)
    completed: list[str] = []

    async def wait(s: float, name: str) -> None:
        await clock.sleep(s)
        completed.append(name)

    tasks = [
        asyncio.create_task(wait(3, "last")),
        asyncio.create_task(wait(1, "first")),
        asyncio.create_task(wait(1, "second")),
    ]
    await asyncio.sleep(0)
    clock.advance(0.5)
    await asyncio.sleep(0)
    assert completed == []
    clock.advance(0.5)
    await asyncio.sleep(0)
    assert completed == ["first", "second"]
    assert clock.now() == 11
    clock.advance(2)
    await asyncio.gather(*tasks)
    assert completed == ["first", "second", "last"]
    assert clock.now() == 13
    assert isinstance(clock, Clock)


async def test_fake_clock_handles_cancelled_sleep_and_zero_wait() -> None:
    clock = FakeClock()
    cancelled = asyncio.create_task(clock.sleep(1))
    due = asyncio.create_task(clock.sleep(1))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await clock.sleep(0)
    assert not due.done()
    clock.advance(1)
    await due
    assert clock.now() == 1


@pytest.mark.parametrize("duration", [-1, math.inf, math.nan])
async def test_clocks_reject_invalid_durations(duration: float) -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(duration)
    with pytest.raises(ValueError):
        await clock.sleep(duration)
    with pytest.raises(ValueError):
        await SystemClock().sleep(duration)


async def test_fake_clock_rejects_overflow_and_nonfinite_start() -> None:
    with pytest.raises(ValueError):
        FakeClock(math.inf)
    with pytest.raises(ValueError):
        FakeClock(1e308).advance(1e308)
    with pytest.raises(ValueError):
        await FakeClock(1e308).sleep(1e308)


async def test_system_clock_is_monotonic() -> None:
    clock = SystemClock()
    before = clock.now()
    await clock.sleep(0.001)
    assert clock.now() >= before
    assert isinstance(clock, Clock)
