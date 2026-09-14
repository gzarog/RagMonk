from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.retrieval import cache as search_cache


@pytest.fixture
def ragpilot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ragpilot_home"
    monkeypatch.setenv("RAGPILOT_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _disable_background_update_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AppContext.bootstrap()`` (CLI performance improvement plan, Phase
    4) unconditionally calls ``update/background.py``'s
    ``maybe_launch_background_check`` on every real bootstrap -- hundreds
    of existing tests call ``AppContext.bootstrap()`` without touching
    the update subsystem at all, and none of them should spawn a real
    detached subprocess making a real GitHub request. Tests that
    specifically exercise the update subsystem construct/pass their own
    ``UpdatesConfig`` directly (``tests/unit/test_update_background.py``)
    or monkeypatch the hook functions themselves
    (``tests/unit/test_lifecycle_updates.py``), so this default is safe
    to override there.
    """
    monkeypatch.setenv("RAGPILOT_UPDATES__ENABLED", "false")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_search_caches() -> None:
    """``retrieval/cache.py``'s query-result/query-embedding caches are
    deliberately process-global state (see that module's docstring) so
    they actually help a long-lived process like ``ragpilot serve`` --
    but that means they would otherwise leak between test functions
    sharing this one pytest process (e.g. two tests both querying
    "AnimalService" against unrelated fixture databases). Clearing them
    before every test keeps each test's cache state as fresh as a real
    new process would see.
    """
    search_cache.reset_caches()
