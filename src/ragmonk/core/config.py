"""Layered configuration loading.

Precedence, highest wins: CLI overrides > environment variables
(``RAGMONK_`` prefix, ``__`` nesting) > project config
(``./.ragmonk.yaml``) > user config (``<runtime_dir>/config.yaml``) >
built-in defaults.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ragmonk.core import paths
from ragmonk.core.errors import ConfigError
from ragmonk.tokenization.model_identity import MAX_SEQUENCE_TOKENS

_ENV_PREFIX = "RAGMONK_"


class RuntimeConfig(BaseModel):
    log_level: str = "info"
    max_workers: int = 6
    max_memory_mb: int = 4096
    temp_directory: str = "auto"
    # Blueprint section 22: SQLite's page cache size, in MB, applied via
    # PRAGMA cache_size on every connection this process opens -- see
    # storage/sqlite.py.
    sqlite_cache_size_mb: int = 64


class IndexingConfig(BaseModel):
    watch: bool = True
    debounce_ms: int = 2000
    max_file_size_mb: int = 100
    follow_symlinks: bool = False
    hash_algorithm: str = "sha256"
    # Phase 7: how often a network/UNC source is fingerprinted (mtime+size
    # scan) since native filesystem events don't reliably cross a network
    # mount -- see watcher/network.py.
    network_poll_seconds: int = 30
    # Phase 7: how often the daemon's periodic reconciliation timer does a
    # full rescan+diff pass per source as a safety net independent of
    # watcher events, catching whatever a missed/coalesced OS event or a
    # dropped poll tick missed -- see service/daemon.py.
    reconciliation_interval_seconds: int = 900
    # Indexing optimization plan, Phase P4: how many files' code
    # extraction (Tree-sitter parse + entity/relationship extraction --
    # the pure, read-only "prepare" half of ``code.processor``) may run
    # concurrently in a bounded thread pool. ``1`` (the default) is
    # fully serial -- byte-for-byte today's pre-P4 behavior -- matching
    # the plan's own "default to serial until proven safe" and "keep
    # experimental concurrency opt-in pending test and benchmark
    # evidence" rules. The transactional write ("publish") half always
    # runs on the single coordinator writer thread regardless of this
    # setting; see ``indexing/coordinator.py``'s ``_process_queue``.
    code_extraction_workers: int = 1
    # Indexing optimization plan V2, Phase P2: how many files' document
    # extraction (Docling conversion + normalization + chunking -- the
    # potentially slow, CPU/IO-heavy "prepare" half of
    # ``documents.pipeline``) may run concurrently in a bounded thread
    # pool. ``1`` (the default) is fully serial -- byte-for-byte the
    # pre-P2 behavior -- for the same reason ``code_extraction_workers``
    # defaults to ``1``: no benchmark evidence yet justifies a riskier
    # default given Docling/OCR/Torch's own native thread pools are far
    # heavier than Tree-sitter's. The bounded in-flight queue this
    # enables (at most this many files' prepared-but-not-yet-published
    # results held in memory) is also what keeps concurrent extraction's
    # peak memory bounded relative to ``runtime.max_memory_mb`` -- see
    # ``indexing/coordinator.py``'s ``_process_queue_with_parallel``. The
    # transactional write ("publish") half always runs on the single
    # coordinator writer thread regardless of this setting, exactly like
    # ``code_extraction_workers``.
    document_extraction_workers: int = 1
    # Indexing optimization plan, Phase P5: how many texts
    # ``retrieval/embedder.py`` sends through the model in one forward
    # pass. Kept configurable (rather than the prior hardcoded constant)
    # so it can be tuned per-machine, but the default is unchanged from
    # before this phase -- CPU-only batching showed no reliable gain past
    # it in this project's own benchmark, so nothing is claimed here that
    # measurement didn't back up (see docs/indexing_benchmarks.md).
    embedding_batch_size: int = 16


class ChunkingConfig(BaseModel):
    """Exact Tokenizer plan, Phase 2: token-aware, hierarchy-aware chunk
    boundaries (``documents/chunker.py``), budgeted against the *exact*
    tokenizer of the embedding model (``ragmonk.tokenization``) rather
    than the earlier approximate estimator.

    ``strategy`` is validated but only ``"hybrid"`` (token-budget packing
    that respects heading/table boundaries -- see that module) is
    implemented so far; the field exists now so a future strategy can be
    added without another config migration.

    ``max_tokens`` is now the model's total input ceiling (including its
    special tokens and the contextual header), not just the body length.
    It defaults to ``"auto"``, which resolves to the pinned embedding
    model's real maximum sequence length (``MAX_SEQUENCE_TOKENS`` -- 256
    for all-MiniLM-L6-v2). An explicit integer is accepted but may never
    exceed that model limit (a larger value would let a chunk be silently
    truncated at embedding time, exactly what this plan removes).
    ``safety_tokens`` is a reserve kept below the model limit so the final
    payload validation has headroom. ``min_tokens``/``overlap_tokens`` are
    soft targets ``merge_peers``/splitting aim for, in *exact* body
    tokens; ``max_tokens`` (minus ``safety_tokens``) is the one hard
    ceiling every non-atomic chunk's full contextual payload must respect.
    A table too large for one chunk is split at row boundaries instead;
    the one remaining atomic exception is a single table cell that alone
    still exceeds the budget -- see ``chunker.chunk_document``'s docstring
    and ``documents/table_renderer``.
    """

    strategy: str = "hybrid"
    max_tokens: int | Literal["auto"] = "auto"
    min_tokens: int = 60
    overlap_tokens: int = 40
    safety_tokens: int = 4
    merge_peers: bool = True

    @field_validator("strategy")
    @classmethod
    def _validate_strategy(cls, value: str) -> str:
        allowed = {"hybrid"}
        if value not in allowed:
            raise ValueError(
                f"unknown documents.chunking.strategy {value!r}; expected one of {sorted(allowed)}"
            )
        return value

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, value: int | str) -> int | str:
        if value == "auto":
            return value
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                "documents.chunking.max_tokens must be the string 'auto' or an integer"
            )
        if value < 16:
            raise ValueError("documents.chunking.max_tokens must be at least 16")
        if value > MAX_SEQUENCE_TOKENS:
            raise ValueError(
                f"documents.chunking.max_tokens ({value}) exceeds the embedding model's "
                f"maximum sequence length ({MAX_SEQUENCE_TOKENS}); a larger value would "
                f"let chunks be silently truncated at embedding time. Use 'auto' or a "
                f"value <= {MAX_SEQUENCE_TOKENS}."
            )
        return value

    @field_validator("min_tokens")
    @classmethod
    def _validate_min_tokens(cls, value: int) -> int:
        if value < 1:
            raise ValueError("documents.chunking.min_tokens must be at least 1")
        return value

    @field_validator("overlap_tokens")
    @classmethod
    def _validate_overlap_tokens(cls, value: int) -> int:
        if value < 0:
            raise ValueError("documents.chunking.overlap_tokens must not be negative")
        return value

    @field_validator("safety_tokens")
    @classmethod
    def _validate_safety_tokens(cls, value: int) -> int:
        if value < 0:
            raise ValueError("documents.chunking.safety_tokens must not be negative")
        return value

    @property
    def resolved_max_tokens(self) -> int:
        """The concrete model input ceiling, resolving ``"auto"`` to the
        pinned embedding model's ``MAX_SEQUENCE_TOKENS``.
        """
        if self.max_tokens == "auto":
            return MAX_SEQUENCE_TOKENS
        return self.max_tokens

    @model_validator(mode="after")
    def _validate_relative_bounds(self) -> ChunkingConfig:
        resolved = self.resolved_max_tokens
        if self.min_tokens > resolved:
            raise ValueError(
                "documents.chunking.min_tokens must not exceed documents.chunking.max_tokens"
            )
        if self.overlap_tokens >= resolved:
            raise ValueError(
                "documents.chunking.overlap_tokens must be less than documents.chunking.max_tokens"
            )
        if self.safety_tokens >= resolved:
            raise ValueError(
                "documents.chunking.safety_tokens must be less than documents.chunking.max_tokens"
            )
        return self


class DocumentsConfig(BaseModel):
    """``ocr`` (Search Quality Improvement Plan, Phase 5) controls
    ``documents/docling_adapter.py``'s PDF OCR fallback: ``"off"`` never
    runs OCR (Phase 3's original, only behavior); ``"auto"`` (the default)
    runs Docling's plain PDF pipeline first and only re-runs it with OCR
    enabled when that first pass looks like it missed real text (see
    ``docling_adapter._should_ocr``); ``"always"`` skips the detection
    pass and runs OCR unconditionally, since a caller who already wants
    OCR gains nothing from paying for two pipeline runs.
    """

    enabled: bool = True
    ocr: str = "auto"
    max_pages: int = 1000
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    # Search Quality Improvement Plan, Phase 10: a raw image
    # (.png/.jpg/.jpeg/.tif/.tiff) has no embedded text layer at all, so
    # -- unlike PDF's "off"/"auto"/"always" -- there is no cheap,
    # non-OCR pass to try first, or to fall back to when OCR doesn't
    # help: real extraction always means a full OCR pass per image. That
    # is meaningfully heavier than every other format this project
    # converts (rule-based, no ML inference), so it defaults to
    # disabled -- an image-kind file is still indexed (as a file record)
    # with no derived document content, exactly like an unsupported
    # extension, until a project opts in. See docling_adapter.py's
    # module docstring for why this reuses the same OCR engine Phase 5
    # already wired up for scanned PDFs.
    image_ocr: bool = False

    @field_validator("ocr")
    @classmethod
    def _validate_ocr(cls, value: str) -> str:
        allowed = {"off", "auto", "always"}
        if value not in allowed:
            raise ValueError(
                f"unknown documents.ocr {value!r}; expected one of {sorted(allowed)}"
            )
        return value


class CodeConfig(BaseModel):
    enabled: bool = True


class SearchVectorConfig(BaseModel):
    """Phase 3's ANN backend selection and rebuild policy (blueprint
    section 31/32). ``engine="auto"`` tries ``usearch`` first and falls
    back to the pure-Python brute-force scan if it can't be loaded (a
    broken install, an unsupported platform) -- see ``retrieval/ann.py``.
    Vectors are always L2-normalized (``retrieval/embedder.py``), so
    there is no separate ``normalize`` toggle: it was never actually a
    choice to expose.
    """

    engine: str = "auto"  # "auto" | "usearch" | "bruteforce"
    rebuild_deleted_ratio: float = 0.15


class SearchCacheConfig(BaseModel):
    """Phase 6's query-result and query-embedding caches (blueprint
    sections 23/24) -- see ``retrieval/cache.py`` for why both are only
    ever useful in a long-lived process (``ragmonk serve``), never a
    one-shot CLI invocation.
    """

    enabled: bool = True
    max_queries: int = 256
    max_query_embeddings: int = 256


class SearchOutputConfig(BaseModel):
    """Controls ``ragmonk search``'s default (no ``--json``/``--snippets``/
    ``--table`` flag) rendering, and what one document hit falls back to
    when a real match-centered snippet isn't available -- both driven by
    this same ordered list, so "configurable fallback" has one meaning
    rather than two. ``fallback[0]`` is the effective default mode; a
    document hit that lacks a usable ``SearchResult.snippet`` (in
    "snippets" mode) degrades to whichever mode comes next in this same
    list, and finally to just its file path if the list is exhausted --
    nothing a search matched is ever silently dropped. Code/entity hits
    are unaffected either way: they always render via their existing
    title/path/tier row (see ``cli/search.py``).
    """

    fallback: list[str] = Field(default_factory=lambda: ["snippets", "json", "files"])
    # SQLite FTS5's own ``snippet()`` hard limit is 1-64 tokens -- see
    # ``storage/repositories/documents_repo.py::search_fts_projection``.
    snippet_max_tokens: int = 32

    @field_validator("fallback")
    @classmethod
    def _validate_fallback(cls, value: list[str]) -> list[str]:
        allowed = {"snippets", "json", "files", "table"}
        if not value:
            raise ValueError("search.output.fallback must not be empty")
        for mode in value:
            if mode not in allowed:
                raise ValueError(
                    f"unknown search.output.fallback mode {mode!r}; "
                    f"expected one of {sorted(allowed)}"
                )
        return value

    @field_validator("snippet_max_tokens")
    @classmethod
    def _validate_snippet_max_tokens(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError(
                "search.output.snippet_max_tokens must be between 1 and 64 "
                "(SQLite FTS5's own snippet() limit)"
            )
        return value


class SearchContextConfig(BaseModel):
    """Search Quality Improvement Plan, Phase 9: how much surrounding
    context a matched paragraph/table chunk is expanded with (nearest
    parent heading, previous/next sibling chunks under that same
    heading) -- a presentation step applied strictly *after* a hit is
    already selected and ranked, never a ranking input (see
    ``retrieval/context_builder.py::expand_chunk_context``).

    ``previous_chunks``/``next_chunks`` at ``0`` (and ``parent_heading``
    at ``False``) disable that piece outright rather than needing a
    caller to special-case "no expansion" -- the same budget/lookup code
    path runs either way, it just has nothing to fetch.
    """

    parent_heading: bool = True
    previous_chunks: int = 1
    next_chunks: int = 1
    max_tokens: int = 1200

    @field_validator("previous_chunks", "next_chunks")
    @classmethod
    def _validate_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("search.context.previous_chunks/next_chunks must be >= 0")
        return value

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("search.context.max_tokens must be > 0")
        return value


class SearchRerankerConfig(BaseModel):
    """Search Quality Improvement Plan, Phase 11 -- explicitly OPTIONAL in
    the plan, and NOT enabled by default. An optional final neural
    reranking pass applied to ``retrieval/reranker.py``'s already RRF-
    fused hybrid tier, reordering only its own top ``top_n`` hits by a
    real local cross-encoder score before the caller's own ``--limit``
    slices the result (see ``retrieval/neural_reranker.py``'s module
    docstring for the model, batching, caching, and graceful-fallback
    contract).

    ``enabled=False`` is a hard requirement for this phase: the plan's own
    promotion gate (>= 5% MRR improvement AND an acceptable warm-query p95
    latency increase) is a later decision, not this one -- see
    CHANGELOG.md's Phase 11 entry for what was actually measured. Turning
    this on never changes ``ragmonk search``'s default (non-``--hybrid``)
    output at all: the neural pass only ever reorders the additive
    ``--hybrid`` view's ranked hits, the same way ``--hybrid`` itself is
    opt-in.
    """

    enabled: bool = False
    top_n: int = 20

    @field_validator("top_n")
    @classmethod
    def _validate_top_n(cls, value: int) -> int:
        if value < 1:
            raise ValueError("search.reranker.top_n must be >= 1")
        return value


class SearchConfig(BaseModel):
    lexical: bool = True
    graph: bool = True
    semantic: bool = False
    # Blueprint section 18: skip semantic search entirely once the
    # lexical pass already found a high-confidence hit (an exact/
    # qualified/alias symbol or an exact title match). Defaults to
    # ``False`` -- ``search.semantic``'s own existing contract is "when
    # this is on, ``ragmonk search``/``explore`` always attach a
    # semantic section", and this project's semantic-retrieval test
    # suite pins that behavior; opting into ``lazy_semantic`` trades a
    # bit of that always-attached guarantee for lower latency on queries
    # the lexical pass already nailed.
    lazy_semantic: bool = False
    # Candidate budget (blueprint section 20): how many nearest
    # neighbors the ANN/brute-force backend is asked for internally,
    # independent of ``--limit``'s final display count.
    semantic_top_k: int = 30
    vector: SearchVectorConfig = Field(default_factory=SearchVectorConfig)
    cache: SearchCacheConfig = Field(default_factory=SearchCacheConfig)
    output: SearchOutputConfig = Field(default_factory=SearchOutputConfig)
    context: SearchContextConfig = Field(default_factory=SearchContextConfig)
    reranker: SearchRerankerConfig = Field(default_factory=SearchRerankerConfig)


class ContextConfig(BaseModel):
    """Phase 5's context-builder budget (blueprint section 27) -- caps how
    much evidence ``retrieval/context_builder.py`` will hand back to a
    caller (``explore`` now, Phase 6's MCP tools later) in one response.
    """

    max_chars: int = 30000
    max_files: int = 20
    max_graph_nodes: int = 100


class McpConfig(BaseModel):
    enabled: bool = True
    # Wall-clock budget for one MCP tool call (blueprint: "timeout
    # enforcement"). Applied via ``asyncio.wait_for`` around the
    # synchronous retrieval call -- see ``mcp/tools.py``.
    request_timeout_seconds: float = 30.0


class ApiConfig(BaseModel):
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8765


class PrivacyConfig(BaseModel):
    external_ai_allowed: bool = False


class CodexAiConfig(BaseModel):
    """Subscription plan, Phase 1: settings for the ``codex`` provider
    (ChatGPT via the official Codex runtime). Deliberately holds **no**
    credential -- account sign-in is delegated entirely to the runtime
    (``ragmonk ai login codex``), never stored in this plain-text config.
    ``auth_mode`` is fixed to ``"chatgpt"`` for now; it exists as a typed,
    validated field so the intended mode is explicit and a future mode can
    be added without a config migration. There is deliberately no
    ``api_key``/executable-path field here: a project-controlled config
    must never be able to select an arbitrary binary to run.
    """

    auth_mode: str = "chatgpt"

    @field_validator("auth_mode")
    @classmethod
    def _validate_auth_mode(cls, value: str) -> str:
        allowed = {"chatgpt"}
        if value not in allowed:
            raise ValueError(
                f"unknown ai.codex.auth_mode {value!r}; expected one of {sorted(allowed)}"
            )
        return value


class GithubCopilotAiConfig(BaseModel):
    """Subscription plan, Phase 1: settings for the ``github_copilot``
    provider. Like ``CodexAiConfig``, stores no credential -- it relies on
    the signed-in Copilot CLI credentials the official SDK reads, never a
    token in this file, and never ``GH_TOKEN``/``GITHUB_TOKEN`` picked up
    by accident (the adapter verifies the effective mode). ``auth_mode`` is
    validated but currently only ``"signed_in_user"`` is supported.
    """

    auth_mode: str = "signed_in_user"

    @field_validator("auth_mode")
    @classmethod
    def _validate_auth_mode(cls, value: str) -> str:
        allowed = {"signed_in_user"}
        if value not in allowed:
            raise ValueError(
                f"unknown ai.github_copilot.auth_mode {value!r}; "
                f"expected one of {sorted(allowed)}"
            )
        return value


class AiConfig(BaseModel):
    """Phase 9's ``ragmonk ask`` provider selection. ``provider="none"``
    (the default) means no provider is configured at all -- ``ragmonk
    ask`` fails with a clear ``ConfigError`` rather than guessing one, the
    same "explicit opt-in, no default guess" stance ``search.semantic``
    takes for embeddings. API keys are never stored here: they are read
    from environment variables at call time (``OPENAI_API_KEY``,
    ``ANTHROPIC_API_KEY``, see ``ai/factory.py``) -- acceptable for CI per
    the blueprint, and it keeps a credential out of ``config.yaml``/
    ``.ragmonk.yaml``, both of which are plain, unencrypted files a
    backup/restore or a careless ``git add`` could otherwise leak.

    Subscription plan, Phase 1: adds ``codex`` and ``github_copilot`` as
    selectable providers, each with its own typed, credential-free
    sub-config. ``provider`` is intentionally *not* validated to a closed
    set here (``ai/factory.py`` raises a clear ``AiNotConfiguredError`` for
    an unknown one at call time) -- the same lazy, call-time validation the
    existing providers already rely on.
    """

    # "none" | "openai" | "anthropic" | "ollama" | "openai_compatible"
    # | "codex" | "github_copilot"
    provider: str = "none"
    model: str = ""
    base_url: str | None = None
    timeout_seconds: float = 60.0
    codex: CodexAiConfig = Field(default_factory=CodexAiConfig)
    github_copilot: GithubCopilotAiConfig = Field(default_factory=GithubCopilotAiConfig)


class TelemetryConfig(BaseModel):
    anonymous_usage: bool = False


class UpdatesConfig(BaseModel):
    """CLI performance improvement plan, Phase 4: automatic update
    checking/notification. ``enabled=false`` disables both the background
    check (``update/background.py``) and the startup notification
    (``update/notifier.py``) entirely -- useful for enterprise/offline
    environments (see the plan's Configuration/Offline Behavior
    sections). ``channel`` is reserved for a future non-stable release
    channel; this repository only ever publishes one today, so it is
    accepted and stored but not yet acted on.
    """

    enabled: bool = True
    check_interval_hours: int = 24
    notify: bool = True
    channel: str = "stable"


class RagMonkConfig(BaseModel):
    version: int = 1
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    code: CodeConfig = Field(default_factory=CodeConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)


_KNOWN_SECTIONS = {
    "version",
    "runtime",
    "indexing",
    "documents",
    "code",
    "search",
    "context",
    "mcp",
    "api",
    "privacy",
    "telemetry",
    "ai",
    "updates",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce_scalar(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"failed to read config file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")
    return data


def _env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    source: Mapping[str, str] = os.environ if environ is None else environ
    overrides: dict[str, Any] = {}
    for key, value in source.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        remainder = key[len(_ENV_PREFIX) :]
        if "__" not in remainder:
            continue
        segments = [seg.lower() for seg in remainder.split("__") if seg]
        if not segments or segments[0] not in _KNOWN_SECTIONS:
            continue
        cursor = overrides
        for segment in segments[:-1]:
            cursor = cursor.setdefault(segment, {})
        cursor[segments[-1]] = _coerce_scalar(value)
    return overrides


def load_config(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RagMonkConfig:
    defaults = RagMonkConfig().model_dump(mode="json")
    user_layer = _load_yaml_file(paths.user_config_path(home))
    project_layer = _load_yaml_file(paths.project_config_path(cwd))
    env_layer = _env_overrides(environ)

    merged = _deep_merge(defaults, user_layer)
    merged = _deep_merge(merged, project_layer)
    merged = _deep_merge(merged, env_layer)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    try:
        return RagMonkConfig.model_validate(merged)
    except Exception as exc:  # pydantic.ValidationError, kept broad for CLI-facing message
        raise ConfigError(f"invalid configuration: {exc}") from exc


def write_user_config(config: RagMonkConfig, *, home: Path | None = None) -> Path:
    path = paths.user_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    path.write_text(dumped, encoding="utf-8")
    return path
