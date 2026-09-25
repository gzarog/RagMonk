"""Shared harness for server-mode end-to-end tests (completion plan
Step 0 / F1-F4, F6).

Writes a real ``storage.mode: server`` config and routes the adapter's
client construction (``build_client`` -- the only place a real
``opensearch-py``/``elasticsearch`` client would be created) to an
in-memory fake engine, so every command runs through the genuine
factory -> adapter -> bulk/search code path with no live cluster.
The fake is shared across every ``AppContext`` a test opens, exactly
like a real cluster would be.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from tests.unit._fake_elasticsearch import FakeElasticsearch
from tests.unit._fake_opensearch import FakeOpenSearch
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core.config import RagMonkConfig, ServerStorageConfig, StorageConfig, write_user_config

ENGINES = ("opensearch", "elasticsearch")


def install_server_mode(
    home: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> Any:
    """Write a server-mode config under ``home`` and patch ``engine``'s
    client factory to return one shared fake. Returns the fake.
    """
    fake: Any = FakeOpenSearch() if engine == "opensearch" else FakeElasticsearch()
    module = f"ragmonk.backends.{engine}"
    monkeypatch.setattr(f"{module}.build_client", lambda **_kwargs: fake)
    write_user_config(
        RagMonkConfig(
            storage=StorageConfig(
                mode="server",
                server=ServerStorageConfig(
                    engine=engine,  # type: ignore[arg-type]
                    url=f"http://{engine}.test:9200",
                    index_prefix="rmtest",
                ),
            )
        ),
        home=home,
    )
    return fake


def add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = re.search(r"Added source (\S+)", result.output)
    assert match is not None, result.output
    return match.group(1)


def write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "billing.py").write_text(
        "def compute_invoice_total(amount):\n"
        "    return apply_tax(amount)\n"
        "\n"
        "def apply_tax(amount):\n"
        "    return amount * 1.2\n"
    )
    (root / "test_billing.py").write_text(
        "from billing import compute_invoice_total\n"
        "\n"
        "def test_total():\n"
        "    assert compute_invoice_total(10) == 12\n"
    )
    (root / "README.md").write_text(
        "# Billing\n\nThe `compute_invoice_total` function returns the invoice total.\n"
    )


def docs(fake: Any) -> list[dict[str, Any]]:
    """Every stored document across every index of ``fake``."""
    return [src for index in fake.store.values() for src in index.values()]
