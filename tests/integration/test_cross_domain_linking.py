"""End-to-end: index a small project mixing code and documents where a
doc explicitly mentions a code symbol by qualified name and by filename,
run ``ragmonk index``, and confirm ``ragmonk link list --json`` shows
the expected auto-discovered cross-domain relationships with correct
confidence -- and that a manually-added link via ``ragmonk link add``
also appears and survives a re-index.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "animal_service.py").write_text(
        "class AnimalService:\n"
        "    def bark_loudly(self):\n"
        "        return 'WOOF'\n"
    )

    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "# API Reference\n\n"
        "The services.animal_service.AnimalService.bark_loudly method "
        "returns the sound.\n\n"
        "See services/animal_service.py for the implementation.\n"
    )


def _links(runner: CliRunner) -> list[dict]:
    result = runner.invoke(app, ["link", "list", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]["links"]


def test_cross_domain_auto_linking_and_manual_link_persist(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    assert "linked=" in index_result.output

    links = _links(runner)
    assert links != []

    qualified_hits = [
        row
        for row in links
        if row["resolver"] == "linker:qualified_identifier"
        and row["entity"] == "services.animal_service.AnimalService.bark_loudly"
    ]
    assert len(qualified_hits) == 1
    qualified_hit = qualified_hits[0]
    assert qualified_hit["confidence"] == "high"
    assert qualified_hit["link_type"] == "documented_by"
    assert qualified_hit["document_path"] is not None
    assert qualified_hit["document_path"].endswith("api.md")

    filename_hits = [row for row in links if row["resolver"] == "linker:filename"]
    assert len(filename_hits) >= 1
    assert filename_hits[0]["confidence"] == "medium"
    assert filename_hits[0]["link_type"] == "mentioned_in"

    # No auto-discovered link is ever EXACT -- that tier is reserved for
    # explicit user mappings (see knowledge/linker.py's module docstring).
    assert all(row["confidence"] != "exact" for row in links)

    add_result = runner.invoke(
        app,
        ["link", "add", "services.animal_service.AnimalService.bark_loudly", "api.md"],
    )
    assert add_result.exit_code == 0, add_result.output

    links_after_add = _links(runner)
    user_links = [row for row in links_after_add if row["resolver"] == "user"]
    assert len(user_links) == 1
    user_link = user_links[0]
    assert user_link["confidence"] == "exact"
    assert user_link["entity"] == "services.animal_service.AnimalService.bark_loudly"
    assert user_link["document_path"].endswith("api.md")

    # Re-index with nothing changed (all files UNCHANGED, so neither side
    # is reprocessed/regenerated) -- the manual link must still be there,
    # untouched, and the auto-discovered links must still be there too.
    reindex_result = runner.invoke(app, ["index"])
    assert reindex_result.exit_code == 0, reindex_result.output

    links_final = _links(runner)
    user_links_final = [row for row in links_final if row["resolver"] == "user"]
    assert len(user_links_final) == 1
    assert user_links_final[0]["link_id"] == user_link["link_id"]

    qualified_hits_final = [
        row
        for row in links_final
        if row["resolver"] == "linker:qualified_identifier"
        and row["entity"] == "services.animal_service.AnimalService.bark_loudly"
    ]
    assert len(qualified_hits_final) == 1
