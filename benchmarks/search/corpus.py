"""Synthetic corpus generation for search benchmarks (blueprint section
35). Entities/documents/embeddings are written straight through the
storage repositories in one transaction -- no Tree-sitter/Docling
parsing, no real embedding model -- so even the "very_large" (1M) corpus
size is generation-bound by SQLite/Python, not by parsing or ML
inference. Every generated corpus also seeds one deterministic "known"
entity/document/path so exact/qualified/alias/path benchmark queries
always have a guaranteed correct hit regardless of corpus size.
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import (
    Document,
    DocumentFormat,
    EmbeddingSubjectType,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragmonk.retrieval import ann
from ragmonk.retrieval import embedder as _embedder
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragmonk.storage.sqlite import transaction

# Blueprint section 35's suggested corpus sizes, counted in total
# embeddable subjects (entities + document paragraphs combined) rather
# than raw file count -- that is what actually drives lexical/ANN
# search cost.
CORPUS_SIZES: dict[str, int] = {
    "small": 5_000,
    "medium": 50_000,
    "large": 250_000,
    "very_large": 1_000_000,
}

# Reuses the real embedder's model id (never its actual model/network
# call -- see fake_embedder.py) so that ``retrieval/semantic.py``'s
# lookups, keyed by ``embedder.EMBEDDING_MODEL_ID``, find these
# synthetic rows without needing a separate code path or config knob.
EMBEDDING_MODEL_ID = _embedder.EMBEDDING_MODEL_ID
EMBEDDING_DIM = 32

_KNOWN_ENTITY_NAME = "SettlementService"
_KNOWN_ENTITY_QUALIFIED = f"services.settlement_service.{_KNOWN_ENTITY_NAME}"
# Mirrors the blueprint's own alias example (section 7): a qualified name
# with a namespace prefix long enough that only its last two segments
# ("SettlementService.Process") form the alias entities_repo.compute_alias
# actually indexes.
_KNOWN_ALIAS_METHOD = "Process"
_KNOWN_ALIAS_QUALIFIED = f"Betsson.Sportsbook.{_KNOWN_ENTITY_NAME}.{_KNOWN_ALIAS_METHOD}"
_KNOWN_PATH = "src/services/settlement_service.py"
_KNOWN_DOCUMENT_TITLE = "Settlement Guide"
_KNOWN_DOCUMENT_PATH = "docs/settlement_guide.md"

_KEYWORD_VOCAB = ("settlement", "retry", "timeout", "payment", "provider", "ledger")


@dataclass(frozen=True)
class KnownFixtures:
    """The one deterministic entity/document/path every generated corpus
    contains, regardless of size -- what the benchmark query set
    (``queries.py``) actually searches for.
    """

    entity_name: str = _KNOWN_ENTITY_NAME
    entity_qualified_name: str = _KNOWN_ENTITY_QUALIFIED
    entity_alias: str = f"{_KNOWN_ENTITY_NAME}.{_KNOWN_ALIAS_METHOD}"
    path_fragment: str = "settlement_service"
    document_title: str = _KNOWN_DOCUMENT_TITLE


@dataclass(frozen=True)
class GeneratedCorpus:
    ctx: AppContext
    project_id: str
    source_path: Path
    size_name: str
    target_count: int
    entity_count: int
    document_paragraph_count: int
    known: KnownFixtures
    generation_seconds: float

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.project_conn(self.project_id)

    def close(self) -> None:
        self.ctx.close()


def _entity_text(index: int, rng: random.Random) -> str:
    words = [rng.choice(_KEYWORD_VOCAB) for _ in range(3)]
    return f"def method_{index}(self): # {' '.join(words)}"


def _insert_known_entity(
    conn: sqlite3.Connection,
    *,
    entity: Entity,
    file_id: str,
    source_id: str,
    model_id: str,
    rng: random.Random,
) -> None:
    entities_repo.insert(conn, entity, snippet=entity.signature or "")
    vector = _random_unit_vector(rng, EMBEDDING_DIM)
    embeddings_repo.insert(
        conn,
        subject_type=EmbeddingSubjectType.ENTITY,
        subject_id=entity.id,
        file_id=file_id,
        source_id=source_id,
        model_id=model_id,
        vector=vector,
    )
    vector_items_repo.insert(
        conn,
        subject_type="entity",
        subject_id=entity.id,
        file_id=file_id,
        source_id=source_id,
        model_id=model_id,
    )


def _seed_known_fixtures(
    conn: sqlite3.Connection, file_id: str, source_id: str, model_id: str, rng: random.Random
) -> None:
    """The class entity backs the exact-symbol (``SettlementService``) and
    qualified-symbol (``services.settlement_service.SettlementService``)
    benchmark queries; the alias entity backs the alias-lookup query
    (``SettlementService.Process``), matching the blueprint's own alias
    example.
    """
    now = datetime.now(UTC).isoformat()
    class_entity = Entity(
        id="benchmark-known-entity",
        source_id=source_id,
        file_id=file_id,
        kind=EntityType.CLASS,
        name=_KNOWN_ENTITY_NAME,
        qualified_name=_KNOWN_ENTITY_QUALIFIED,
        language="python",
        signature=f"class {_KNOWN_ENTITY_NAME}: # settlement retry timeout",
        start_line=1,
        end_line=20,
        generation=1,
        created_at=now,
        updated_at=now,
    )
    _insert_known_entity(
        conn, entity=class_entity, file_id=file_id, source_id=source_id, model_id=model_id, rng=rng
    )

    alias_entity = Entity(
        id="benchmark-known-alias-entity",
        source_id=source_id,
        file_id=file_id,
        kind=EntityType.METHOD,
        name=_KNOWN_ALIAS_METHOD,
        qualified_name=_KNOWN_ALIAS_QUALIFIED,
        language="python",
        signature=f"def {_KNOWN_ALIAS_METHOD}(self): # settlement retry timeout",
        start_line=21,
        end_line=25,
        generation=1,
        created_at=now,
        updated_at=now,
    )
    _insert_known_entity(
        conn, entity=alias_entity, file_id=file_id, source_id=source_id, model_id=model_id, rng=rng
    )


def _random_unit_vector(rng: random.Random, dim: int) -> list[float]:
    raw = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in raw) ** 0.5
    if norm == 0.0:
        raw[0] = 1.0
        norm = 1.0
    return [v / norm for v in raw]


def generate(
    home: Path,
    *,
    size_name: str,
    seed: int = 0,
    source_root: Path | None = None,
) -> GeneratedCorpus:
    """Builds a fresh RagMonk project (a real ``knowledge.db``, real
    project id/paths) containing ``CORPUS_SIZES[size_name]`` embeddable
    subjects, ~70% code entities / ~30% document paragraphs, spread
    across many synthetic files/documents, plus the fixed
    ``KnownFixtures`` every benchmark query targets. Also builds the
    persistent ANN index for the generated embeddings, matching what a
    real ``ragmonk index`` run with ``search.semantic`` on would leave
    behind.
    """
    if size_name not in CORPUS_SIZES:
        raise ValueError(f"unknown corpus size {size_name!r}; choose one of {sorted(CORPUS_SIZES)}")
    target_count = CORPUS_SIZES[size_name]
    rng = random.Random(seed)
    started = time.perf_counter()

    project_root = source_root or (home / "_benchmark_source" / size_name)
    project_root.mkdir(parents=True, exist_ok=True)

    ctx = AppContext.bootstrap(
        home=home,
        cwd=project_root,
        cli_overrides={"search": {"semantic": True, "cache": {"enabled": False}}},
    )
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    source = registry.add(str(project_root))
    project_id = paths.project_id_for_path(project_root)
    conn = ctx.project_conn(project_id)

    entity_target = int(target_count * 0.7)
    paragraph_target = target_count - entity_target
    entities_per_file = 20
    paragraphs_per_document = 10
    now = datetime.now(UTC).isoformat()

    # ``files_repo.insert`` opens its own transaction internally (unlike
    # ``entities_repo``/``documents_repo``/``embeddings_repo``/
    # ``vector_items_repo``, which are caller-transaction-managed) --
    # each file's row is committed on its own, and its entities/
    # paragraphs/embeddings/vector_items are batched into one
    # transaction per file/document rather than one per row, keeping
    # even the "very_large" (1M) corpus size's insert count of
    # transactions manageable.
    known_file_id = "benchmark-known-file"
    files_repo.insert(
        conn,
        FileRecord(
            id=known_file_id,
            source_id=source.id,
            path=_KNOWN_PATH,
            kind=FileKind.CODE,
            size=1,
            mtime=0.0,
            status=FileStatus.INDEXED,
            created_at=now,
            updated_at=now,
        ),
    )
    with transaction(conn):
        _seed_known_fixtures(conn, known_file_id, source.id, EMBEDDING_MODEL_ID, rng)

    entities_written = 0
    file_index = 0
    while entities_written < entity_target:
        file_id = f"code-file-{file_index}"
        files_repo.insert(
            conn,
            FileRecord(
                id=file_id,
                source_id=source.id,
                path=f"src/pkg{file_index % 50}/module_{file_index}.py",
                kind=FileKind.CODE,
                size=1,
                mtime=0.0,
                status=FileStatus.INDEXED,
                created_at=now,
                updated_at=now,
            ),
        )
        with transaction(conn):
            for local_index in range(min(entities_per_file, entity_target - entities_written)):
                index = entities_written
                entity = Entity(
                    id=f"benchmark-entity-{index}",
                    source_id=source.id,
                    file_id=file_id,
                    kind=EntityType.METHOD,
                    name=f"method_{index}",
                    qualified_name=(
                        f"pkg{file_index % 50}.Module{file_index}.Class{local_index}.method_{index}"
                    ),
                    language="python",
                    signature=_entity_text(index, rng),
                    start_line=local_index * 5 + 1,
                    end_line=local_index * 5 + 4,
                    generation=1,
                    created_at=now,
                    updated_at=now,
                )
                entities_repo.insert(conn, entity, snippet=entity.signature or "")
                vector = _random_unit_vector(rng, EMBEDDING_DIM)
                embeddings_repo.insert(
                    conn,
                    subject_type=EmbeddingSubjectType.ENTITY,
                    subject_id=entity.id,
                    file_id=file_id,
                    source_id=source.id,
                    model_id=EMBEDDING_MODEL_ID,
                    vector=vector,
                )
                vector_items_repo.insert(
                    conn,
                    subject_type="entity",
                    subject_id=entity.id,
                    file_id=file_id,
                    source_id=source.id,
                    model_id=EMBEDDING_MODEL_ID,
                )
                entities_written += 1
        file_index += 1

    known_doc_file_id = "benchmark-known-doc-file"
    files_repo.insert(
        conn,
        FileRecord(
            id=known_doc_file_id,
            source_id=source.id,
            path=_KNOWN_DOCUMENT_PATH,
            kind=FileKind.DOCUMENT,
            size=1,
            mtime=0.0,
            status=FileStatus.INDEXED,
            created_at=now,
            updated_at=now,
        ),
    )
    with transaction(conn):
        known_document = Document(
            id="benchmark-known-document",
            source_id=source.id,
            file_id=known_doc_file_id,
            format=DocumentFormat.MARKDOWN,
            title=_KNOWN_DOCUMENT_TITLE,
            generation=1,
            created_at=now,
            updated_at=now,
        )
        documents_repo.insert_document(conn, known_document)
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id="benchmark-known-paragraph",
                document_id=known_document.id,
                file_id=known_doc_file_id,
                text=(
                    "Provider settlements are retried automatically by the "
                    "retry worker whenever a payment attempt times out."
                ),
                order_index=0,
                generation=1,
                created_at=now,
            ),
            doc_title=_KNOWN_DOCUMENT_TITLE,
        )

    paragraphs_written = 0
    doc_index = 0
    while paragraphs_written < paragraph_target:
        doc_file_id = f"doc-file-{doc_index}"
        files_repo.insert(
            conn,
            FileRecord(
                id=doc_file_id,
                source_id=source.id,
                path=f"docs/topic{doc_index % 50}/note_{doc_index}.md",
                kind=FileKind.DOCUMENT,
                size=1,
                mtime=0.0,
                status=FileStatus.INDEXED,
                created_at=now,
                updated_at=now,
            ),
        )
        with transaction(conn):
            document = Document(
                id=f"benchmark-document-{doc_index}",
                source_id=source.id,
                file_id=doc_file_id,
                format=DocumentFormat.MARKDOWN,
                title=f"Note {doc_index}",
                generation=1,
                created_at=now,
                updated_at=now,
            )
            documents_repo.insert_document(conn, document)
            for local_index in range(
                min(paragraphs_per_document, paragraph_target - paragraphs_written)
            ):
                index = paragraphs_written
                words = [rng.choice(_KEYWORD_VOCAB) for _ in range(6)]
                paragraph = Paragraph(
                    id=f"benchmark-paragraph-{index}",
                    document_id=document.id,
                    file_id=doc_file_id,
                    text=f"Paragraph {index} discusses {' '.join(words)} in detail.",
                    order_index=local_index,
                    generation=1,
                    created_at=now,
                )
                documents_repo.insert_paragraph(conn, paragraph, doc_title=document.title or "")
                vector = _random_unit_vector(rng, EMBEDDING_DIM)
                embeddings_repo.insert(
                    conn,
                    subject_type=EmbeddingSubjectType.DOCUMENT_SECTION,
                    subject_id=paragraph.id,
                    file_id=doc_file_id,
                    source_id=source.id,
                    model_id=EMBEDDING_MODEL_ID,
                    vector=vector,
                )
                vector_items_repo.insert(
                    conn,
                    subject_type="document_section",
                    subject_id=paragraph.id,
                    file_id=doc_file_id,
                    source_id=source.id,
                    model_id=EMBEDDING_MODEL_ID,
                )
                paragraphs_written += 1
        doc_index += 1

    dim = embeddings_repo.get_dim_for_model(conn, EMBEDDING_MODEL_ID) or EMBEDDING_DIM
    ann.rebuild_index(
        conn,
        project_id=project_id,
        home=ctx.home,
        engine=ctx.config.search.vector.engine,
        ndim=dim,
        model_id=EMBEDDING_MODEL_ID,
    )

    elapsed = time.perf_counter() - started
    return GeneratedCorpus(
        ctx=ctx,
        project_id=project_id,
        source_path=project_root,
        size_name=size_name,
        target_count=target_count,
        entity_count=entities_written,
        document_paragraph_count=paragraphs_written,
        known=KnownFixtures(),
        generation_seconds=elapsed,
    )
