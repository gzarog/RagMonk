"""``ai/runtime.py``: the sync/async bridge and the lazy runtime loader.

No real subscription runtime exists in Phase 1, so ``resolve_runtime``
must report ``runtime_unavailable`` for the subscription providers -- and
the ``RuntimeBridge`` itself is exercised with plain coroutines, never a
real child process, so these stay fast and platform-independent.
"""

from __future__ import annotations

import asyncio

import pytest

from ragmonk.ai.base import AiRuntimeUnavailableError, AiTimeoutError
from ragmonk.ai.runtime import RuntimeBridge, RuntimeStatus, resolve_runtime
from ragmonk.core.config import AiConfig


def test_bridge_runs_a_coroutine_and_returns_its_result() -> None:
    bridge = RuntimeBridge()
    try:

        async def add() -> int:
            await asyncio.sleep(0)
            return 41 + 1

        assert bridge.run(add(), timeout=5.0) == 42
    finally:
        bridge.close()


def test_bridge_propagates_exceptions_from_the_coroutine() -> None:
    bridge = RuntimeBridge()
    try:

        async def boom() -> None:
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            bridge.run(boom(), timeout=5.0)
    finally:
        bridge.close()


def test_bridge_raises_ai_timeout_when_coroutine_exceeds_deadline() -> None:
    bridge = RuntimeBridge()
    try:

        async def slow() -> None:
            await asyncio.sleep(10)

        with pytest.raises(AiTimeoutError):
            bridge.run(slow(), timeout=0.1)
    finally:
        bridge.close()


def test_bridge_works_even_when_called_from_within_a_running_loop() -> None:
    """The MCP-server case: a synchronous ``run`` invoked from inside an
    already-running event loop must not deadlock or call ``asyncio.run``
    re-entrantly. Running the blocking call in a worker thread mirrors how
    the real provider would be invoked off the server loop.
    """
    bridge = RuntimeBridge()

    async def outer() -> int:
        async def inner() -> int:
            await asyncio.sleep(0)
            return 7

        return await asyncio.to_thread(bridge.run, inner(), timeout=5.0)

    try:
        assert asyncio.run(outer()) == 7
    finally:
        bridge.close()


def test_bridge_close_is_idempotent_and_safe_when_never_started() -> None:
    bridge = RuntimeBridge()
    bridge.close()
    bridge.close()  # no error on second close


def test_resolve_runtime_reports_unavailable_for_codex_in_phase_1() -> None:
    with pytest.raises(AiRuntimeUnavailableError):
        resolve_runtime("codex", ai=AiConfig())


def test_resolve_runtime_rejects_a_non_subscription_provider() -> None:
    with pytest.raises(AiRuntimeUnavailableError):
        resolve_runtime("openai", ai=AiConfig())


def test_runtime_status_to_dict_carries_no_secret_fields() -> None:
    state = RuntimeStatus(provider_id="codex", authenticated=True, account="user@example.com")
    data = state.to_dict()
    assert data["provider"] == "codex"
    assert data["authenticated"] is True
    assert set(data) == {"provider", "authenticated", "account", "runtime_version", "detail"}
