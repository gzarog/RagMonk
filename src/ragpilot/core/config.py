"""Layered configuration loading.

Precedence, highest wins: CLI overrides > environment variables
(``RAGPILOT_`` prefix, ``__`` nesting) > project config
(``./.ragpilot.yaml``) > user config (``<runtime_dir>/config.yaml``) >
built-in defaults.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from ragpilot.core import paths
from ragpilot.core.errors import ConfigError

_ENV_PREFIX = "RAGPILOT_"


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


class DocumentsConfig(BaseModel):
    enabled: bool = True
    ocr: str = "auto"
    max_pages: int = 1000


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
    ever useful in a long-lived process (``ragpilot serve``), never a
    one-shot CLI invocation.
    """

    enabled: bool = True
    max_queries: int = 256
    max_query_embeddings: int = 256


class SearchOutputConfig(BaseModel):
    """Controls ``ragpilot search``'s default (no ``--json``/``--snippets``/
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


class SearchConfig(BaseModel):
    lexical: bool = True
    graph: bool = True
    semantic: bool = False
    # Blueprint section 18: skip semantic search entirely once the
    # lexical pass already found a high-confidence hit (an exact/
    # qualified/alias symbol or an exact title match). Defaults to
    # ``False`` -- ``search.semantic``'s own existing contract is "when
    # this is on, ``ragpilot search``/``explore`` always attach a
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


class AiConfig(BaseModel):
    """Phase 9's ``ragpilot ask`` provider selection. ``provider="none"``
    (the default) means no provider is configured at all -- ``ragpilot
    ask`` fails with a clear ``ConfigError`` rather than guessing one, the
    same "explicit opt-in, no default guess" stance ``search.semantic``
    takes for embeddings. API keys are never stored here: they are read
    from environment variables at call time (``OPENAI_API_KEY``,
    ``ANTHROPIC_API_KEY``, see ``ai/factory.py``) -- acceptable for CI per
    the blueprint, and it keeps a credential out of ``config.yaml``/
    ``.ragpilot.yaml``, both of which are plain, unencrypted files a
    backup/restore or a careless ``git add`` could otherwise leak.
    """

    provider: str = "none"  # "none" | "openai" | "anthropic" | "ollama" | "openai_compatible"
    model: str = ""
    base_url: str | None = None
    timeout_seconds: float = 60.0


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


class RagpilotConfig(BaseModel):
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
) -> RagpilotConfig:
    defaults = RagpilotConfig().model_dump(mode="json")
    user_layer = _load_yaml_file(paths.user_config_path(home))
    project_layer = _load_yaml_file(paths.project_config_path(cwd))
    env_layer = _env_overrides(environ)

    merged = _deep_merge(defaults, user_layer)
    merged = _deep_merge(merged, project_layer)
    merged = _deep_merge(merged, env_layer)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    try:
        return RagpilotConfig.model_validate(merged)
    except Exception as exc:  # pydantic.ValidationError, kept broad for CLI-facing message
        raise ConfigError(f"invalid configuration: {exc}") from exc


def write_user_config(config: RagpilotConfig, *, home: Path | None = None) -> Path:
    path = paths.user_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    path.write_text(dumped, encoding="utf-8")
    return path
