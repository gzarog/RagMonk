"""Completion plan F4: the targeted read primitives added to
``KnowledgeBackend`` are implemented by every real backend (local,
OpenSearch, Elasticsearch), and the server implementations return the
published generation only.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit._fake_elasticsearch import FakeElasticsearch
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend
from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.backends.models import (
    FileRecord,
    LinkCandidate,
    PreparedCode,
    PreparedDocument,
    PreparedLinks,
)
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.core.config import ServerStorageConfig
from ragmonk.core.models import (
    Confidence,
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    RelationshipType,
)
from ragmonk.documents.chunker import Chunk

_PRIMITIVES = (
    "get_files",
    "list_files",
    "get_entities",
    "list_entities",
    "list_source_entities",
    "find_entities_by_names",
    "get_links",
    "remove_link",
    "get_documents",
    "list_documents",
    "get_document_units",
    "list_source_document_units",
    "find_relationships_by_target_prefix",
)


@pytest.mark.parametrize(
    "cls", [LocalKnowledgeBackend, OpenSearchKnowledgeBackend, ElasticsearchKnowledgeBackend]
)
def test_every_real_backend_implements_the_read_contract(cls: type) -> None:
    for name in _PRIMITIVES:
        assert getattr(cls, name) is not getattr(KnowledgeBackend, name), (cls, name)


def _server_backend(engine: str) -> Any:
    config = ServerStorageConfig(engine=engine, url="http://x:9200")  # type: ignore[arg-type]
    if engine == "opensearch":
        return OpenSearchKnowledgeBackend(config, client=FakeOpenSearch())
    return ElasticsearchKnowledgeBackend(config, client=FakeElasticsearch())


def _entity(entity_id: str, name: str, file_id: str, generation: int) -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=f"mod.{name}",
        language="python",
        signature=f"def {name}()",
        start_line=3,
        end_line=4,
        generation=generation,
        created_at="t",
        updated_at="t",
    )


def _publish_generation(backend: Any, generation: int, suffix: str) -> None:
    backend.upsert_files(
        [
            FileRecord(
                file_id=f"fc{suffix}", source_id="s1", path=f"src/code{suffix}.py",
                content_hash="h", generation=generation,
            ),
            FileRecord(
                file_id=f"fd{suffix}", source_id="s1", path=f"docs/doc{suffix}.md",
                content_hash="h", generation=generation,
            ),
        ]
    )
    backend.publish_code(
        PreparedCode(
            file_id=f"fc{suffix}",
            source_id="s1",
            generation=generation,
            entities=[_entity(f"e{suffix}", f"fn{suffix}", f"fc{suffix}", generation)],
        )
    )
    document = Document(
        id=f"d{suffix}", source_id="s1", file_id=f"fd{suffix}", format=DocumentFormat.MARKDOWN,
        title=f"Doc {suffix}", section_count=1, generation=generation, created_at="t",
        updated_at="t",
    )
    chunk = Chunk(
        kind="paragraph", text=f"about fn{suffix}", heading_level=None, heading_path=("Intro",),
        parent_index=None, page_start=2, page_end=2, contextual_text="ctx",
        search_text=f"about fn{suffix}",
    )
    backend.publish_document(
        PreparedDocument(
            file_id=f"fd{suffix}", source_id="s1", generation=generation, document=document,
            chunk_ids=[f"u{suffix}"], chunks=[chunk], doc_title=f"Doc {suffix}",
        )
    )
    backend.publish_links(
        PreparedLinks(
            source_id="s1",
            generation=generation,
            candidates=[
                LinkCandidate(
                    entity_id=f"e{suffix}", document_id=f"d{suffix}", section_id=f"u{suffix}",
                    link_type=RelationshipType.MENTIONED_IN, resolver="linker:exact_identifier",
                    confidence=Confidence.EXACT, evidence=f"fn{suffix}",
                )
            ],
        )
    )


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_server_targeted_reads_return_published_generation_only(engine: str) -> None:
    backend = _server_backend(engine)
    backend.ensure_schema()
    g1 = backend.begin_generation("s1")
    _publish_generation(backend, int(g1), "A")
    backend.publish_generation("s1", g1)
    g2 = backend.begin_generation("s1")
    _publish_generation(backend, int(g2), "B")  # written, NOT published

    assert [e.name for e in backend.get_entities(["eA", "eB"])] == ["fnA"]
    entity = backend.get_entities(["eA"])[0]
    assert entity.signature == "def fnA()" and entity.start_line == 3
    assert sorted(f.path for f in backend.list_files("s1")) == ["docs/docA.md", "src/codeA.py"]
    assert [f.path for f in backend.get_files(["fcA", "fcB"])] == ["src/codeA.py"]
    links = backend.get_links(entity_ids=["eA", "eB"])
    assert [(lk.entity_id, lk.document_id, lk.section_id) for lk in links] == [("eA", "dA", "uA")]
    assert backend.get_links(document_ids=["dA"])[0].confidence == "exact"
    docs = backend.list_documents(source_id="s1")
    assert [(d.document_id, d.title, d.section_count) for d in docs] == [("dA", "Doc A", 1)]
    assert backend.get_documents(["dB"]) == []
    units = backend.get_document_units(document_id="dA")
    assert [(u.unit_id, u.heading_path, u.page_start) for u in units] == [("uA", ["Intro"], 2)]
    assert [u.unit_id for u in backend.get_document_units(unit_ids=["uA", "uB"])] == ["uA"]
    assert [e.name for e in backend.list_entities(query="fnA")] == ["fnA"]
    assert [e.name for e in backend.find_entities_by_names(names=["fnA", "fnB"])] == ["fnA"]
    # The writer of generation 2 can read its own, unpublished artifacts.
    assert [
        e.name for e in backend.list_source_entities("s1", generation=g2)
    ] == ["fnB"]

    backend.publish_generation("s1", g2)
    assert [e.name for e in backend.get_entities(["eA", "eB"])] == ["fnB"]
    assert sorted(f.path for f in backend.list_files("s1")) == ["docs/docB.md", "src/codeB.py"]
    # GC: nothing of generation 1 remains anywhere.
    stored = [src for idx in backend._get_client().store.values() for src in idx.values()]
    assert {s.get("generation") for s in stored if s.get("generation")} == {g2}


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_server_abort_generation_removes_files_and_links(engine: str) -> None:
    backend = _server_backend(engine)
    backend.ensure_schema()
    g1 = backend.begin_generation("s1")
    _publish_generation(backend, int(g1), "A")
    backend.publish_generation("s1", g1)
    g2 = backend.begin_generation("s1")
    _publish_generation(backend, int(g2), "B")
    backend.abort_generation("s1", g2)
    stored = [src for idx in backend._get_client().store.values() for src in idx.values()]
    assert {s.get("generation") for s in stored if s.get("generation")} == {g1}
    assert {s.get("doc_kind") for s in stored if s.get("generation") == g1} >= {
        "file",
        "entity",
        "document",
        "chunk",
        "link",
    }


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_server_delete_file_removes_links_of_its_entities(engine: str) -> None:
    backend = _server_backend(engine)
    backend.ensure_schema()
    g1 = backend.begin_generation("s1")
    _publish_generation(backend, int(g1), "A")
    backend.publish_generation("s1", g1)
    assert backend.get_links(entity_ids=["eA"])
    backend.delete_file("s1", "fcA")
    assert backend.get_links(entity_ids=["eA"]) == []
    assert backend.get_entities(["eA"]) == []
    assert [f.file_id for f in backend.list_files("s1")] == ["fdA"]


def test_local_backend_targeted_reads(ragmonk_home: Any, tmp_path: Any, runner: Any) -> None:
    """Local-mode side of the same contract, over a real indexed project."""
    from pathlib import Path

    from ragmonk.cli.main import app
    from ragmonk.core import paths
    from ragmonk.core.lifecycle import AppContext
    from ragmonk.sources.registry import SourceRegistry

    project = Path(tmp_path) / "proj"
    project.mkdir()
    (project / "mod.py").write_text(
        "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n"
    )
    (project / "README.md").write_text("# Guide\n\nCall `alpha` to start.\n")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(project)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    with AppContext.bootstrap() as ctx:
        source = SourceRegistry(ctx.sources_conn, home=ctx.home).list()[0]
        backend = ctx.backend(paths.project_id_for_path(Path(source.path)))
        alpha = backend.find_entities_by_names(names=["alpha"])
        assert [e.name for e in alpha] == ["alpha"]
        assert [e.id for e in backend.get_entities([alpha[0].id])] == [alpha[0].id]
        files = backend.get_files([alpha[0].file_id])
        assert files[0].path.endswith("mod.py")
        assert {Path(f.path).name for f in backend.list_files(source.id)} >= {"mod.py", "README.md"}
        documents = backend.list_documents(source_id=source.id)
        assert documents and backend.get_documents([documents[0].document_id])
        units = backend.get_document_units(document_id=documents[0].document_id)
        assert units
        links = backend.get_links(entity_ids=[alpha[0].id])
        assert links and links[0].document_id == documents[0].document_id
        assert backend.list_entities(limit=10)


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_server_scan_paginates_past_one_page(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_scan`` pages with ``search_after`` -- a source larger than one
    page (here: page size 2) is returned completely, never truncated.
    """
    from ragmonk.backends import server_common

    monkeypatch.setattr(server_common, "_SCAN_PAGE_SIZE", 2)
    backend = _server_backend(engine)
    backend.ensure_schema()
    g1 = backend.begin_generation("s1")
    entities = [_entity(f"e{i}", f"fn{i}", "fc", int(g1)) for i in range(7)]
    backend.publish_code(
        PreparedCode(file_id="fc", source_id="s1", generation=int(g1), entities=entities)
    )
    backend.publish_generation("s1", g1)
    names = sorted(e.name for e in backend.list_source_entities("s1"))
    assert names == sorted(f"fn{i}" for i in range(7))
