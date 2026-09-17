"""Bounded runtime lifecycle and the sync/async bridge (subscription
plan, Phase 1).

Two things live here, both stdlib-only so importing this module starts no
runtime and loads no SDK:

1. ``RuntimeBridge`` -- a single dedicated worker thread that owns one
   asyncio event loop for the lifetime of a runtime object. RagMonk's
   provider contract (``AiProvider.answer``) is synchronous, but the
   subscription runtimes are async and ``ragmonk ask`` may be called both
   from a one-shot CLI (no running loop) *and* from inside the MCP
   server's already-running loop. Submitting each coroutine to this
   dedicated loop via ``run_coroutine_threadsafe`` works identically in
   both cases, so no code path ever calls ``asyncio.run()`` re-entrantly.
   A per-call deadline cancels the coroutine and raises ``AiTimeoutError``.

2. ``SubscriptionRuntime`` and ``resolve_runtime`` -- the lifecycle
   surface (status/login/logout/models) that ``cli/ai.py`` drives, plus
   the lazy loader that turns a provider id into a concrete runtime. In
   Phase 1 no adapter module is present, so ``resolve_runtime`` raises
   ``AiRuntimeUnavailableError`` -- exactly the "runtime unavailable"
   contract Phase 1 must demonstrate. Phase 2 (``ai/codex.py``) and
   Phase 3 (``ai/github_copilot.py``) add the modules this loader finds.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

from ragmonk.ai.base import AiRuntimeUnavailableError, AiTimeoutError
from ragmonk.ai.registry import RUNTIME_MODULES, get_capability
from ragmonk.core.config import AiConfig

_T = TypeVar("_T")


class RuntimeBridge:
    """Owns one event loop on one background thread. ``run`` blocks the
    calling (synchronous) thread until the submitted coroutine completes,
    fails, or the deadline elapses.

    Not started until first use (``run``), so merely constructing a
    provider that holds a bridge spins up no thread -- and so
    ``ragmonk ai providers``/``--help`` never start one.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise RuntimeError("RuntimeBridge has been closed")
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=_run, name="ragmonk-ai-runtime", daemon=True
            )
            thread.start()
            ready.wait()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro: Coroutine[Any, Any, _T], *, timeout: float) -> _T:
        """Submit ``coro`` to the worker loop and block for its result.

        On timeout the coroutine is cancelled on the loop (best effort)
        and ``AiTimeoutError`` is raised, so a caller never blocks past
        the deadline waiting on an unresponsive runtime.
        """
        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise AiTimeoutError(
                f"ai runtime did not respond within {timeout:.0f}s; the request was cancelled"
            ) from exc

    def close(self) -> None:
        """Stop the loop and join the worker thread. Idempotent, and safe
        to call on a bridge that was never started.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        if loop is not None:
            loop.close()


@dataclass(frozen=True)
class RuntimeStatus:
    """A snapshot of one subscription runtime's connection state, safe to
    print: it deliberately carries no token, cookie, or secret-bearing
    URL (release gate 7).
    """

    provider_id: str
    authenticated: bool
    account: str | None = None
    runtime_version: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "authenticated": self.authenticated,
            "account": self.account,
            "runtime_version": self.runtime_version,
            "detail": self.detail,
        }


@runtime_checkable
class SubscriptionRuntime(Protocol):
    """The lifecycle surface ``cli/ai.py`` drives for a subscription
    provider. Distinct from ``AiProvider`` (which only answers): these are
    the connection-management operations (sign in, sign out, inspect
    state, list models) an account-based provider needs and an API-key
    provider does not.
    """

    provider_id: str

    def status(self) -> RuntimeStatus: ...

    def login(self) -> RuntimeStatus: ...

    def logout(self) -> None: ...

    def models(self) -> list[str]: ...

    def close(self) -> None: ...


def resolve_runtime(provider_id: str, *, ai: AiConfig) -> SubscriptionRuntime:
    """Lazily load the concrete runtime for a subscription provider.

    Raises ``AiRuntimeUnavailableError`` when the provider is not a known
    subscription provider, when its adapter module is not present yet
    (Phase 1), or when the module is present but its required runtime/SDK
    cannot be imported -- in every case *before* any child process or
    network client is created.
    """
    provider = provider_id.strip().lower()
    cap = get_capability(provider)
    if cap is None or not cap.subscription:
        raise AiRuntimeUnavailableError(
            f"{provider_id!r} is not a subscription provider with a managed runtime"
        )
    module_path = RUNTIME_MODULES.get(provider)
    if module_path is None:  # pragma: no cover - guarded by the check above
        raise AiRuntimeUnavailableError(f"no runtime is registered for provider {provider_id!r}")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise AiRuntimeUnavailableError(
            f"the {cap.display_name} runtime is not available: {exc}. "
            "Install the required runtime/SDK to use this provider "
            "(see docs/providers)."
        ) from exc
    factory = getattr(module, "create_runtime", None)
    if factory is None:
        raise AiRuntimeUnavailableError(
            f"the {cap.display_name} adapter does not expose a runtime yet"
        )
    return factory(ai)
