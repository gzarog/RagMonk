"""Completion plan F8: full server-mode acceptance against a REAL
OpenSearch / Elasticsearch cluster, through the real CLI.

Marked ``opensearch_integration`` / ``elasticsearch_integration`` (per
engine parameter) and therefore excluded from the default ``pytest``
run. Point it at a cluster with ``OPENSEARCH_URL`` / ``ELASTICSEARCH_URL``
(plus the usual ``RAGMONK_<ENGINE>_USERNAME``/``_PASSWORD``/``_API_KEY``
if security is enabled):

    OPENSEARCH_URL=http://localhost:9200 pytest -m opensearch_integration \\
        tests/integration/test_server_acceptance_live.py

Without a reachable cluster the tests SKIP -- unless
``RAGMONK_REQUIRE_LIVE_SERVER=1`` is set (CI sets it), in which case an
unreachable cluster FAILS the run instead of silently passing.

Covered, in order: init (real engine validation), source add, initial
index, lexical search, symbol/callers/callees/references/impact/explore,
incremental add/modify/delete/rename, outage handling (no local
fallback), a failed rebuild that preserves the previous generation, a
successful atomic rebuild, doctor/status, and source remove. Semantic/
hybrid search runs only with ``RAGMONK_ACCEPTANCE_SEMANTIC=1`` (it needs
the local embedding model).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration._server_harness import add_source, write_project
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths

_ENV_URL = {"opensearch": "OPENSEARCH_URL", "elasticsearch": "ELASTICSEARCH_URL"}


def _reachable(engine: str, url: str) -> bool:
    if not url:
        return False
    try:
        from ragmonk.backends.validation import validate_server_config
        from ragmonk.core.config import ServerStorageConfig

        validate_server_config(
            ServerStorageConfig(engine=engine, url=url, verify_tls=False)  # type: ignore[arg-type]
        )
        return True
    except Exception:
        return False


@pytest.fixture(
    params=[
        pytest.param("opensearch", marks=pytest.mark.opensearch_integration),
        pytest.param("elasticsearch", marks=pytest.mark.elasticsearch_integration),
    ]
)
def live_engine(request: pytest.FixtureRequest) -> Iterator[tuple[str, str]]:
    engine = str(request.param)
    module = "opensearchpy" if engine == "opensearch" else "elasticsearch"
    url = os.environ.get(_ENV_URL[engine], "")
    required = os.environ.get("RAGMONK_REQUIRE_LIVE_SERVER") == "1"
    try:
        __import__(module)
    except ImportError:
        if required:
            pytest.fail(f"{module} is not installed but a live {engine} run was required")
        pytest.skip(f"{module} not installed")
    if not _reachable(engine, url):
        message = f"no reachable, valid {engine} cluster at ${_ENV_URL[engine]} ({url or 'unset'})"
        if required:
            pytest.fail(message)
        pytest.skip(message)
    yield engine, url


def _cli(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(app, list(args))


def _ok(result: Any) -> str:
    assert result.exit_code == 0, result.output
    return str(result.output)


def _json(result: Any) -> dict[str, Any]:
    payload = json.loads(_ok(result))
    data: dict[str, Any] = payload.get("data", payload)
    return data


def _delete_prefix(engine: str, url: str, prefix: str) -> None:
    from ragmonk.backends.validation import build_engine_client
    from ragmonk.core.config import ServerStorageConfig

    client = build_engine_client(
        ServerStorageConfig(engine=engine, url=url, verify_tls=False)  # type: ignore[arg-type]
    )
    for suffix in ("files", "content", "relationships"):
        with contextlib.suppress(Exception):  # best-effort cleanup
            client.indices.delete(index=f"{prefix}-{suffix}")


def test_live_server_acceptance(
    live_engine: tuple[str, str],
    ragmonk_home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, url = live_engine
    prefix = f"rm-accept-{uuid.uuid4().hex[:8]}"
    project = tmp_path / "proj"
    write_project(project)
    try:
        # -- init with real engine validation ---------------------------
        _ok(
            _cli(
                runner, "init", "--storage-mode", "server", "--storage-engine", engine,
                "--storage-url", url, "--storage-index-prefix", prefix,
                "--storage-no-verify-tls",
            )
        )
        source_id = add_source(runner, project)

        # -- initial index + lexical search -----------------------------
        _ok(_cli(runner, "index"))
        results = _json(_cli(runner, "search", "compute_invoice_total", "--json"))["results"]
        assert any(r["path"].endswith("billing.py") for r in results), results

        # -- code intelligence ------------------------------------------
        for command in ("symbol", "callers", "callees", "references"):
            _ok(_cli(runner, command, "compute_invoice_total"))
        callees = _json(_cli(runner, "callees", "compute_invoice_total", "--json"))
        assert any(e["target_entity_id"] for e in callees["edges"]), callees
        impact = _json(_cli(runner, "impact", "compute_invoice_total", "--json"))
        assert impact["defined"] and impact["documentation"]
        assert "README.md" in _ok(_cli(runner, "explore", "compute_invoice_total"))

        # -- incremental add / modify / delete / rename ------------------
        (project / "extra.py").write_text("def brand_new_helper():\n    return 1\n")
        (project / "billing.py").write_text(
            "def compute_invoice_total(amount):\n    return amount\n\n"
            "def renamed_tax(amount):\n    return amount\n"
        )
        _ok(_cli(runner, "index"))
        assert _json(_cli(runner, "symbol", "brand_new_helper", "--json"))["matches"]
        assert _json(_cli(runner, "symbol", "renamed_tax", "--json"))["matches"]
        assert not _json(_cli(runner, "symbol", "apply_tax", "--json"))["matches"]
        (project / "extra.py").rename(project / "moved.py")
        _ok(_cli(runner, "index"))
        moved = _json(_cli(runner, "search", "brand_new_helper", "--json"))["results"]
        assert any(r["path"].endswith("moved.py") for r in moved), moved
        (project / "moved.py").unlink()
        _ok(_cli(runner, "index"))
        assert not _json(_cli(runner, "symbol", "brand_new_helper", "--json"))["matches"]

        # -- failed rebuild keeps the previous generation ----------------
        before = _json(_cli(runner, "search", "compute_invoice_total", "--json"))["results"]
        module = __import__(f"ragmonk.backends.{engine}", fromlist=["run_bulk_or_raise"])
        original = module.run_bulk_or_raise
        calls = {"n": 0}

        def _flaky(client: Any, actions: Any, config: Any) -> Any:
            calls["n"] += 1
            if calls["n"] >= 2:
                from ragmonk.core.errors import DatabaseError

                raise DatabaseError("injected bulk failure")
            return original(client, actions, config)

        monkeypatch.setattr(module, "run_bulk_or_raise", _flaky)
        assert _cli(runner, "rebuild", "--source", source_id).exit_code != 0
        monkeypatch.setattr(module, "run_bulk_or_raise", original)
        after_failed = _json(_cli(runner, "search", "compute_invoice_total", "--json"))["results"]
        assert sorted(r["id"] for r in after_failed) == sorted(r["id"] for r in before)

        # -- successful atomic rebuild -----------------------------------
        _ok(_cli(runner, "rebuild", "--source", source_id))
        assert _json(_cli(runner, "symbol", "renamed_tax", "--json"))["matches"]

        # -- doctor / status ----------------------------------------------
        _ok(_cli(runner, "status"))
        doctor = _cli(runner, "doctor", "--json")
        assert engine in doctor.output.lower()

        # -- outage: no silent local fallback -----------------------------
        config_path = paths.user_config_path(ragmonk_home)
        original_config = config_path.read_text()
        config_path.write_text(original_config.replace(url, "http://127.0.0.1:1"))
        outage = _cli(runner, "search", "compute_invoice_total", "--json")
        assert outage.exit_code != 0
        config_path.write_text(original_config)
        _ok(_cli(runner, "search", "compute_invoice_total"))

        # -- source remove purges the server --------------------------------
        _ok(_cli(runner, "source", "remove", source_id, "--yes"))
        assert _json(_cli(runner, "search", "compute_invoice_total", "--json"))["results"] == []
    finally:
        _delete_prefix(engine, url, prefix)


def test_live_semantic_and_hybrid_search(
    live_engine: tuple[str, str],
    ragmonk_home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("RAGMONK_ACCEPTANCE_SEMANTIC") != "1":
        pytest.skip("set RAGMONK_ACCEPTANCE_SEMANTIC=1 (downloads the embedding model)")
    engine, url = live_engine
    prefix = f"rm-accept-sem-{uuid.uuid4().hex[:8]}"
    project = tmp_path / "proj"
    write_project(project)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")
    try:
        _ok(
            _cli(
                runner, "init", "--storage-mode", "server", "--storage-engine", engine,
                "--storage-url", url, "--storage-index-prefix", prefix,
                "--storage-no-verify-tls",
            )
        )
        add_source(runner, project)
        _ok(_cli(runner, "index"))
        data = _json(_cli(runner, "search", "how is the invoice total computed", "--json",
                          "--hybrid"))
        semantic = data.get("semantic") or {}
        assert semantic.get("available") is True, data
        assert semantic.get("results"), data
    finally:
        _delete_prefix(engine, url, prefix)
