"""Generate RUST-01 golden fixtures from the Python reference.

Run from the repository root with the Python reference installed:

    python rust/compat/tools/gen_core_golden.py

Writes rust/compat/golden/core_*.json. Rust tests in ragmonk-config,
ragmonk-core and ragmonk-telemetry replay every case and must match
exactly. Regenerate only deliberately (see ADR 0001).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import yaml

from ragmonk.backends import elasticsearch_ids, opensearch_ids
from ragmonk.backends.factory import redact_url, redact_urls_in_text
from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig, load_config, url_has_userinfo
from ragmonk.core.errors import RagMonkError, RunLockTimeoutError
from ragmonk.security.secrets import is_secret_filename
from ragmonk.sources.registry import detect_source_type, make_source_id

OUT = Path(__file__).resolve().parents[1] / "golden"

# --- config -----------------------------------------------------------------

YAML_CASES: list[tuple[str, str]] = [
    ("empty", ""),
    ("comment_only", "# nothing\n"),
    ("null_doc", "~\n"),
    ("top_level_list", "- a\n- b\n"),
    ("top_level_scalar", "hello\n"),
    ("unknown_keys_ignored", "bogus: 1\nruntime:\n  bogus: 2\n  max_workers: 3\n"),
    ("int_from_str", "runtime:\n  max_workers: '5'\n"),
    ("int_from_padded_str", "runtime:\n  max_workers: ' 5 '\n"),
    ("int_from_float", "runtime:\n  max_workers: 5.0\n"),
    ("int_from_fraction", "runtime:\n  max_workers: 5.5\n"),
    ("int_from_bool", "runtime:\n  max_workers: true\n"),
    ("int_from_float_str", "runtime:\n  max_workers: '5.0'\n"),
    ("int_from_frac_str", "runtime:\n  max_workers: '5.5'\n"),
    ("int_null", "runtime:\n  max_workers: null\n"),
    ("int_garbage", "runtime:\n  max_workers: abc\n"),
    ("int_underscore_str", "runtime:\n  max_workers: '1_000'\n"),
    ("int_plus_str", "runtime:\n  max_workers: '+5'\n"),
    ("int_hex_str", "runtime:\n  max_workers: '0x10'\n"),
    ("int_list", "runtime:\n  max_workers: [1]\n"),
    ("yaml11_octal", "runtime:\n  max_workers: 012\n"),
    ("yaml11_hex", "runtime:\n  max_workers: 0x1A\n"),
    ("yaml11_binary", "runtime:\n  max_workers: 0b101\n"),
    ("yaml11_underscore_int", "runtime:\n  max_memory_mb: 1_024\n"),
    ("yaml11_sexagesimal", "runtime:\n  max_workers: 1:30\n"),
    ("yaml11_negative", "runtime:\n  max_workers: -3\n"),
    ("str_from_int", "runtime:\n  log_level: 5\n"),
    ("str_from_bool_on", "runtime:\n  log_level: on\n"),
    ("str_from_yes", "runtime:\n  log_level: yes\n"),
    ("str_from_null", "runtime:\n  log_level: null\n"),
    ("str_from_tilde", "runtime:\n  log_level: ~\n"),
    ("str_from_date", "runtime:\n  log_level: 2024-01-02\n"),
    ("str_from_float", "runtime:\n  log_level: 1.5\n"),
    ("str_quoted_on", "runtime:\n  log_level: 'on'\n"),
    ("str_debug", "runtime:\n  log_level: debug\n"),
    ("str_explicit_tag", "runtime:\n  log_level: !!str 5\n"),
    ("bool_yes_str", "indexing:\n  watch: 'yes'\n"),
    ("bool_yes_plain", "indexing:\n  watch: yes\n"),
    ("bool_off_plain", "indexing:\n  watch: off\n"),
    ("bool_one", "indexing:\n  watch: 1\n"),
    ("bool_two", "indexing:\n  watch: 2\n"),
    ("bool_maybe", "indexing:\n  watch: maybe\n"),
    ("bool_one_float", "indexing:\n  watch: 1.0\n"),
    ("bool_padded", "indexing:\n  watch: '  true '\n"),
    ("bool_upper", "indexing:\n  watch: 'TRUE'\n"),
    ("bool_upper_plain", "indexing:\n  watch: TRUE\n"),
    ("bool_mixed_plain", "indexing:\n  watch: tRUE\n"),
    ("bool_t", "indexing:\n  watch: 't'\n"),
    ("bool_y_plain", "indexing:\n  watch: y\n"),
    ("bool_n_str", "indexing:\n  watch: 'N'\n"),
    ("bool_null", "indexing:\n  watch: null\n"),
    ("float_from_str", "indexing:\n  lock_timeout_seconds: '3'\n"),
    ("float_from_bool", "indexing:\n  lock_timeout_seconds: true\n"),
    ("float_zero", "indexing:\n  lock_timeout_seconds: 0\n"),
    ("float_too_big", "indexing:\n  lock_timeout_seconds: 3601\n"),
    ("float_max_ok", "indexing:\n  lock_timeout_seconds: 3600\n"),
    ("float_inf_str", "indexing:\n  lock_timeout_seconds: 'inf'\n"),
    ("float_yaml_inf", "indexing:\n  status_stall_threshold_seconds: .inf\n"),
    ("float_yaml_nan_mcp", "mcp:\n  request_timeout_seconds: .nan\n"),
    ("float_exp_str", "indexing:\n  lock_timeout_seconds: '1e1'\n"),
    ("float_exp_plain", "indexing:\n  lock_timeout_seconds: 1e1\n"),
    ("float_exp_plain_dot", "indexing:\n  lock_timeout_seconds: 1.0e+1\n"),
    ("float_padded_str", "indexing:\n  lock_timeout_seconds: ' 2 '\n"),
    ("float_garbage", "indexing:\n  lock_timeout_seconds: abc\n"),
    ("float_tiny", "mcp:\n  request_timeout_seconds: 0.000001\n"),
    ("float_big", "mcp:\n  request_timeout_seconds: 12345678901234567890.0\n"),
    ("float_neg_zero", "mcp:\n  request_timeout_seconds: -0.0\n"),
    ("float_third", "mcp:\n  request_timeout_seconds: 0.1\n"),
    ("float_int_value", "mcp:\n  request_timeout_seconds: 45\n"),
    ("stall_zero", "indexing:\n  status_stall_threshold_seconds: 0\n"),
    ("chunk_auto", "documents:\n  chunking:\n    max_tokens: auto\n"),
    ("chunk_int_str", "documents:\n  chunking:\n    max_tokens: '100'\n"),
    ("chunk_float", "documents:\n  chunking:\n    max_tokens: 100.0\n"),
    ("chunk_bool", "documents:\n  chunking:\n    max_tokens: true\n"),
    ("chunk_upper_auto", "documents:\n  chunking:\n    max_tokens: AUTO\n"),
    ("chunk_too_big", "documents:\n  chunking:\n    max_tokens: 300\n"),
    ("chunk_too_small", "documents:\n  chunking:\n    max_tokens: 15\n"),
    ("chunk_min_over_max", "documents:\n  chunking:\n    max_tokens: 50\n    min_tokens: 60\n"),
    ("chunk_overlap_eq_max", "documents:\n  chunking:\n    max_tokens: 40\n    min_tokens: 10\n"),
    ("chunk_safety_big", "documents:\n  chunking:\n    safety_tokens: 256\n"),
    ("chunk_multi_field_errors", "documents:\n  chunking:\n    min_tokens: 0\n    overlap_tokens: -1\n    safety_tokens: -2\n"),
    ("chunk_field_and_model", "documents:\n  chunking:\n    min_tokens: 0\n    max_tokens: 20\n"),
    ("chunk_strategy_bad", "documents:\n  chunking:\n    strategy: semantic\n"),
    ("chunk_max_null", "documents:\n  chunking:\n    max_tokens: null\n"),
    ("chunk_max_list", "documents:\n  chunking:\n    max_tokens: [1]\n"),
    ("docs_ocr_bad", "documents:\n  ocr: sometimes\n"),
    ("docs_ocr_always", "documents:\n  ocr: always\n"),
    ("docs_ocr_off_plain", "documents:\n  ocr: off\n"),
    ("docs_pdf_mode_fast", "documents:\n  pdf_mode: fast\n"),
    ("docs_pdf_mode_bad", "documents:\n  pdf_mode: turbo\n"),
    ("docs_pdf_workers_0", "documents:\n  pdf_process_workers: 0\n"),
    ("docs_pdf_workers_33", "documents:\n  pdf_process_workers: 33\n"),
    ("docs_attach_limits_bad", "documents:\n  email_attachment_max_bytes: 0\n  email_attachment_max_count: -1\n  email_attachment_total_max_bytes: 0\n"),
    ("docs_email_off", "documents:\n  email_attachments: false\n"),
    ("search_fallback_str", "search:\n  output:\n    fallback: json\n"),
    ("search_fallback_mixed", "search:\n  output:\n    fallback: [json, 1]\n"),
    ("search_fallback_empty", "search:\n  output:\n    fallback: []\n"),
    ("search_fallback_bad", "search:\n  output:\n    fallback: [json, html]\n"),
    ("search_fallback_ok", "search:\n  output:\n    fallback: [table, files]\n"),
    ("search_snippet_0", "search:\n  output:\n    snippet_max_tokens: 0\n"),
    ("search_snippet_65", "search:\n  output:\n    snippet_max_tokens: 65\n"),
    ("search_context_neg", "search:\n  context:\n    previous_chunks: -1\n    next_chunks: -2\n    max_tokens: 0\n"),
    ("search_reranker", "search:\n  reranker:\n    enabled: true\n    top_n: 0\n"),
    ("search_semantic_on", "search:\n  semantic: true\n  semantic_top_k: 50\n  vector:\n    engine: bruteforce\n    rebuild_deleted_ratio: 0.5\n"),
    ("ai_base_url_int", "ai:\n  base_url: 5\n"),
    ("ai_base_url_null", "ai:\n  base_url: null\n"),
    ("ai_provider", "ai:\n  provider: openai\n  model: gpt-x\n  base_url: https://api.example.com/v1\n"),
    ("ai_codex_bad", "ai:\n  codex:\n    auth_mode: apikey\n"),
    ("ai_copilot_bad", "ai:\n  github_copilot:\n    auth_mode: token\n"),
    ("section_scalar", "runtime: x\n"),
    ("section_null", "runtime: null\n"),
    ("section_list", "storage: [1, 2]\n"),
    ("nested_section_scalar", "documents:\n  chunking: 5\n"),
    ("version_str", "version: '2'\n"),
    ("version_bad", "version: two\n"),
    ("storage_server", "storage:\n  mode: server\n  server:\n    engine: elasticsearch\n    url: https://es.example.com:9200\n    index_prefix: team\n    verify_tls: false\n    request_timeout_seconds: 10\n    bulk:\n      max_actions: 100\n      max_bytes: 1000\n      concurrency: 4\n      max_retries: 0\n"),
    ("storage_url_creds", "storage:\n  server:\n    url: https://user:hunter2@es.example.com:9200\n"),
    ("storage_url_creds_noscheme", "storage:\n  server:\n    url: user:hunter2@es.example.com:9200\n"),
    ("storage_url_at_in_path", "storage:\n  server:\n    url: https://es.example.com/a@b\n"),
    ("storage_mode_engine_bad", "storage:\n  mode: x\n  server:\n    engine: y\n"),
    ("storage_timeout_zero", "storage:\n  server:\n    request_timeout_seconds: 0\n"),
    ("storage_bulk_bad", "storage:\n  server:\n    bulk:\n      max_actions: 0\n      max_bytes: 0\n      concurrency: 0\n      max_retries: -1\n"),
    ("updates", "updates:\n  enabled: false\n  check_interval_hours: 6\n  notify: false\n  channel: beta\n"),
    ("many_errors_order", "storage:\n  mode: x\nruntime:\n  max_workers: x\n  log_level: 3\nindexing:\n  watch: 7\n"),
    ("anchor_alias", "base: &b\n  max_workers: 9\nruntime: *b\n"),
    ("merge_key", "base: &b\n  max_workers: 9\nruntime:\n  <<: *b\n  log_level: warn\n"),
    ("int_keys_top", "1: x\nruntime:\n  max_workers: 2\n"),
    ("unicode_values", "ai:\n  model: 'μοντέλο – ✓'\n"),
    ("long_string", "ai:\n  model: " + "x" * 120 + "\n"),
    ("long_spaced_string", "ai:\n  model: '" + " ".join(["word"] * 40) + "'\n"),
    ("string_needs_quotes", "ai:\n  model: 'a: b'\n  provider: '#hash'\n"),
    ("string_looks_numeric", "ai:\n  model: '123'\n  provider: '1.5'\n"),
    ("string_leading_space", "ai:\n  model: ' padded'\n"),
    ("string_multiline", "ai:\n  model: \"line1\\nline2\"\n"),
    ("string_with_quote", "ai:\n  model: \"it's\"\n"),
    ("string_empty", "ai:\n  model: ''\n"),
    ("string_null_word", "ai:\n  model: 'null'\n"),
    ("string_tab", "ai:\n  model: \"a\\tb\"\n"),
    ("yaml_syntax_error", "runtime: [\n"),
    ("duplicate_keys", "runtime:\n  max_workers: 1\n  max_workers: 2\n"),
    ("flow_mapping", "runtime: {max_workers: 7, log_level: error}\n"),
    ("block_literal", "ai:\n  model: |\n    abc\n    def\n"),
    ("int_str_exp", "runtime:\n  max_workers: '1e3'\n"),
    ("int_str_trailing_dot", "runtime:\n  max_workers: '5.'\n"),
    ("int_str_neg_zero", "runtime:\n  max_workers: '-0'\n"),
    ("int_str_double_underscore", "runtime:\n  max_workers: '1__0'\n"),
    ("int_str_leading_underscore", "runtime:\n  max_workers: '_1'\n"),
    ("int_str_padded_float", "runtime:\n  max_workers: ' 5.0 '\n"),
    ("int_str_empty", "runtime:\n  max_workers: ''\n"),
    ("int_str_float_zeros", "runtime:\n  max_workers: '5.000'\n"),
    ("int_float_big", "runtime:\n  max_workers: 1.0e+20\n"),
    ("int_inf", "runtime:\n  max_workers: .inf\n"),
    ("float_str_underscore", "mcp:\n  request_timeout_seconds: '1_000.5'\n"),
    ("float_str_infinity", "mcp:\n  request_timeout_seconds: 'Infinity'\n"),
    ("float_str_neg_inf", "mcp:\n  request_timeout_seconds: '-inf'\n"),
    ("float_str_plus", "mcp:\n  request_timeout_seconds: '+1.5'\n"),
    ("float_str_bad_exp", "mcp:\n  request_timeout_seconds: '1e'\n"),
    ("float_str_padded_nan", "mcp:\n  request_timeout_seconds: ' nan '\n"),
    ("float_str_dot5", "mcp:\n  request_timeout_seconds: '.5'\n"),
    ("float_str_empty", "mcp:\n  request_timeout_seconds: ''\n"),
    ("float_null", "mcp:\n  request_timeout_seconds: null\n"),
    ("float_list", "mcp:\n  request_timeout_seconds: [1]\n"),
    ("float_yaml11_sexagesimal", "mcp:\n  request_timeout_seconds: 1:30.5\n"),
    ("float_yaml11_dot_int", "mcp:\n  request_timeout_seconds: 1_0.5\n"),
    ("float_yaml_plain_exp_no_dot", "mcp:\n  request_timeout_seconds: 1e5\n"),
    ("float_repr_1e16", "mcp:\n  request_timeout_seconds: 1.0e+16\n"),
    ("float_repr_1e15", "mcp:\n  request_timeout_seconds: 1000000000000000.0\n"),
    ("float_repr_small", "mcp:\n  request_timeout_seconds: 0.0001\n"),
    ("float_repr_smaller", "mcp:\n  request_timeout_seconds: 0.00001\n"),
    ("float_repr_pi", "mcp:\n  request_timeout_seconds: 3.141592653589793\n"),
    ("bool_yaml11_variants", "indexing:\n  watch: Off\n  follow_symlinks: ON\n"),
    ("bool_str_on", "indexing:\n  watch: 'on'\n  follow_symlinks: 'f'\n"),
    ("bool_float_half", "indexing:\n  watch: 0.5\n"),
    ("bool_list", "indexing:\n  watch: []\n"),
    ("yaml_null_variants", "ai:\n  base_url: Null\n  model: NULL\n"),
    ("yaml_bool_key", "on: 1\nruntime:\n  max_workers: 2\n"),
    ("yaml_timestamp_full", "ai:\n  model: 2001-12-14t21:59:43.10-05:00\n"),
    ("yaml_explicit_int_tag", "runtime:\n  max_workers: !!int '7'\n"),
    ("yaml_explicit_float_tag", "mcp:\n  request_timeout_seconds: !!float '2'\n"),
    ("yaml_value_eq", "ai:\n  model: =\n"),
    ("yaml_merge_list", "a: &a\n  max_workers: 4\nb: &b\n  log_level: warn\nruntime:\n  <<: [*a, *b]\n"),
    ("str_with_hash_space", "ai:\n  model: 'a #b'\n"),
    ("str_colon_end", "ai:\n  model: 'a:'\n"),
    ("str_dash_start", "ai:\n  model: '- a'\n"),
    ("str_question", "ai:\n  model: '?x'\n"),
    ("str_backslash", "ai:\n  model: 'C:\\path\\x'\n"),
    ("str_trailing_space", "ai:\n  model: 'x '\n"),
    ("str_long_quoted_fold", "ai:\n  model: '" + "ab: " * 30 + "'\n"),
    ("str_long_unicode", "ai:\n  model: '" + "é " * 50 + "'\n"),
    ("str_crlf", "ai:\n  model: \"a\\r\\nb\"\n"),
    ("str_control", "ai:\n  model: \"a\\x01b\"\n"),
    ("str_emoji", "ai:\n  model: \"\U0001F600\"\n"),
    ("str_nbsp", "ai:\n  model: \"a\\u00a0b\"\n"),
    ("str_bom", "ai:\n  model: \"\\ufeffx\"\n"),
    ("str_only_spaces", "ai:\n  model: '   '\n"),
    ("str_multiline_trailing", "ai:\n  model: \"a\\n\"\n"),
    ("str_many_newlines", "ai:\n  model: \"a\\n\\n\\nb\"\n"),
    ("str_dot_inf_word", "ai:\n  model: '.inf'\n"),
    ("str_tilde_word", "ai:\n  model: '~'\n"),
    ("str_yes_word", "ai:\n  model: 'yes'\n"),
    ("str_date_word", "ai:\n  model: '2024-01-02'\n"),
    ("str_octal_word", "ai:\n  model: '012'\n"),
    ("str_sexagesimal_word", "ai:\n  model: '1:30'\n"),
    ("str_url", "ai:\n  base_url: 'http://localhost:11434/v1'\n"),
    ("str_at_start", "ai:\n  model: '@x'\n"),
    ("str_backtick", "ai:\n  model: '`x'\n"),
    ("str_percent", "ai:\n  model: '%x'\n"),
    ("str_brackets", "ai:\n  model: 'a[0]{b}'\n"),
    ("str_comma", "ai:\n  model: 'a, b'\n"),
    ("str_ampersand", "ai:\n  model: '&x'\n"),
    ("str_star", "ai:\n  model: '*x'\n"),
    ("str_pipe", "ai:\n  model: '|x'\n"),
    ("str_gt", "ai:\n  model: '>x'\n"),
    ("str_bang", "ai:\n  model: '!x'\n"),
    ("str_quote_start", "ai:\n  model: \"'x\"\n"),
    ("str_dquote_start", "ai:\n  model: '\"x'\n"),
    ("str_doc_start", "ai:\n  model: '---'\n"),
    ("str_doc_end", "ai:\n  model: '...'\n"),
    ("str_long_word_then_space", "ai:\n  model: '" + "y" * 90 + " z'\n"),
    ("bigint", "indexing:\n  debounce_ms: 1180591620717411303424\n"),
]

ENV_CASES: list[tuple[str, dict[str, str]]] = [
    ("env_int", {"RAGMONK_RUNTIME__MAX_WORKERS": "9"}),
    ("env_bool_upper", {"RAGMONK_INDEXING__WATCH": "FALSE"}),
    ("env_bool_padded", {"RAGMONK_INDEXING__WATCH": " true "}),
    ("env_bool_yes", {"RAGMONK_INDEXING__WATCH": "yes"}),
    ("env_float", {"RAGMONK_INDEXING__LOCK_TIMEOUT_SECONDS": "2.5"}),
    ("env_float_inf", {"RAGMONK_MCP__REQUEST_TIMEOUT_SECONDS": "inf"}),
    ("env_int_underscore", {"RAGMONK_RUNTIME__MAX_WORKERS": "1_0"}),
    ("env_int_padded", {"RAGMONK_RUNTIME__MAX_WORKERS": " 7 "}),
    ("env_str_numeric", {"RAGMONK_RUNTIME__LOG_LEVEL": "5"}),
    ("env_str", {"RAGMONK_RUNTIME__LOG_LEVEL": "debug"}),
    ("env_nested", {"RAGMONK_STORAGE__SERVER__URL": "https://h:9200"}),
    ("env_lowercase_key", {"RAGMONK_runtime__max_workers": "4"}),
    ("env_unknown_section", {"RAGMONK_BOGUS__X": "1"}),
    ("env_no_nesting", {"RAGMONK_SEARCH": "x"}),
    ("env_empty_segments", {"RAGMONK_RUNTIME____MAX_WORKERS": "8"}),
    ("env_section_override", {"RAGMONK_RUNTIME__": "x"}),
    ("env_trailing_sep", {"RAGMONK_RUNTIME__MAX_WORKERS__": "3"}),
    ("env_list_field", {"RAGMONK_SEARCH__OUTPUT__FALLBACK": "json"}),
    ("env_version", {"RAGMONK_VERSION__X": "2"}),
    ("env_credential_not_config", {"RAGMONK_OPENSEARCH_PASSWORD": "secret"}),
    ("env_overrides_user", {"RAGMONK_RUNTIME__MAX_WORKERS": "11"}),
    ("env_exp_float", {"RAGMONK_SEARCH__VECTOR__REBUILD_DELETED_RATIO": "1e-1"}),
    ("env_nan", {"RAGMONK_SEARCH__VECTOR__REBUILD_DELETED_RATIO": "nan"}),
    ("env_negative", {"RAGMONK_RUNTIME__MAX_WORKERS": "-2"}),
    ("env_int_exp", {"RAGMONK_RUNTIME__MAX_WORKERS": "1e3"}),
    ("env_bool_mixed", {"RAGMONK_INDEXING__WATCH": "TrUe"}),
    ("env_infinity", {"RAGMONK_MCP__REQUEST_TIMEOUT_SECONDS": "-Infinity"}),
    ("env_float_underscore", {"RAGMONK_MCP__REQUEST_TIMEOUT_SECONDS": "1_0.5"}),
    ("env_empty_value", {"RAGMONK_AI__MODEL": ""}),
    ("env_unicode_digits", {"RAGMONK_RUNTIME__MAX_WORKERS": "\u0663"}),
    ("env_plus_int", {"RAGMONK_RUNTIME__MAX_WORKERS": "+4"}),
    ("env_hex", {"RAGMONK_RUNTIME__MAX_WORKERS": "0x4"}),
    ("env_two_sections", {"RAGMONK_RUNTIME__MAX_WORKERS": "4", "RAGMONK_AI__PROVIDER": "ollama"}),
    ("env_float_for_int", {"RAGMONK_RUNTIME__MAX_WORKERS": "2.0"}),
]

LAYER_CASES = [
    # (name, user_yaml, project_yaml, env)
    ("project_over_user", "runtime:\n  max_workers: 2\n  log_level: warn\n", "runtime:\n  max_workers: 3\n", {}),
    ("env_over_project", "", "runtime:\n  max_workers: 3\n", {"RAGMONK_RUNTIME__MAX_WORKERS": "4"}),
    ("project_scalar_replaces_section", "runtime:\n  max_workers: 2\n", "runtime: 5\n", {}),
    ("project_invalid_toplevel", "", "- x\n", {}),
    ("layer_env_overrides_user", "runtime:\n  max_workers: 2\n", "", {"RAGMONK_RUNTIME__MAX_WORKERS": "11"}),
]


def _dump(config: RagMonkConfig) -> str:
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)


def _run(user: str | None, project: str | None, env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cwd = Path(tmp) / "cwd"
        home.mkdir()
        cwd.mkdir()
        if user is not None:
            (home / "config.yaml").write_text(user, encoding="utf-8")
        if project is not None:
            (cwd / ".ragmonk.yaml").write_text(project, encoding="utf-8")
        try:
            cfg = load_config(home=home, cwd=cwd, environ=env)
        except RagMonkError as exc:
            msg = str(exc).replace(str(home), "<HOME>").replace(str(cwd), "<CWD>")
            return {"error": msg, "exit_code": exc.exit_code}
        return {"yaml": _dump(cfg)}


def gen_config() -> list[dict]:
    cases = [{"name": "defaults", "user": None, "project": None, "env": {}}]
    cases += [{"name": n, "user": y, "project": None, "env": {}} for n, y in YAML_CASES]
    cases += [{"name": n, "user": None, "project": None, "env": e} for n, e in ENV_CASES]
    cases += [{"name": n, "user": u, "project": p, "env": e} for n, u, p, e in LAYER_CASES]
    for case in cases:
        case["expected"] = _run(case["user"], case["project"], case["env"])
    return cases


# --- ids / paths / redaction -----------------------------------------------

def gen_ids() -> dict:
    canonical_paths = [
        "/home/user/repo",
        "/tmp/claude-0/sb/src",
        "C:\\Users\\me\\repo",
        "\\\\server\\share\\repo",
        "/ünïcödé/路径",
        "",
    ]
    server_args = [
        ("src_c6f8289e57", "8e00d378b30d42729c88a87e142406b6"),
        ("src_0000000000", "a"),
        ("src_x", "f\x1fid"),
    ]
    out: dict = {"source_ids": [], "project_ids": [], "server": [], "source_types": []}
    for p in canonical_paths:
        out["source_ids"].append({"canonical_path": p, "id": make_source_id(p)})
        out["project_ids"].append(
            {"canonical_path": p, "id": __import__("hashlib").sha256(p.encode()).hexdigest()[:12]}
        )
    for raw in ["/a/b", "//server/share", "///triple", "\\\\host\\s", "smb://h/s", "nfs://h", "afp://h", "C:\\x", "relative", "http://h"]:
        out["source_types"].append({"raw": raw, "type": str(detect_source_type(raw))})
    for mod_name, mod in (("opensearch", opensearch_ids), ("elasticsearch", elasticsearch_ids)):
        for sid, fid in server_args:
            for gen in (None, "g1"):
                out["server"].append({
                    "module": mod_name, "source_id": sid, "file_id": fid, "generation": gen,
                    "file_doc_id": mod.file_doc_id(sid, fid, gen),
                    "generation_marker_id": mod.generation_marker_id(sid),
                    "document_doc_id": mod.document_doc_id(sid, fid, gen),
                    "document_doc_id_att3": mod.document_doc_id(sid, fid, gen, attachment_index=3),
                    "entity_doc_id": mod.entity_doc_id(sid, fid, "e1"),
                    "chunk_doc_id": mod.chunk_doc_id(sid, fid, "c1"),
                    "relationship_doc_id": mod.relationship_doc_id(sid, fid, "r1"),
                    "link_doc_id": mod.link_doc_id(sid, "e1", "d1", None, "documented_by", "exact_name", gen),
                    "link_doc_id_section": mod.link_doc_id(sid, "e1", "d1", "s9", "mentioned_in", "alias", gen),
                })
    return out


def gen_redaction() -> dict:
    texts = [
        "",
        "plain text",
        "connect https://user:pass@host:9200/x failed",
        "two http://a:b@h1 and ftp://c@h2/p",
        "bare user:pw@host.example.com:9200 here",
        "email me@example.com stays",
        "path /a/b:c@d stays?",
        "x:y@[::1]:9200",
        "word_user:pw@host",
        "already https://host:9200 clean",
        "ünï:pw@hóst",
        "a:b@ c",
        "SECRETVALUE leaked in message",
        "short abc leaked",
    ]
    env = {"RAGMONK_OPENSEARCH_PASSWORD": "SECRETVALUE", "RAGMONK_ELASTICSEARCH_API_KEY": "abc"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        text_cases = [{"input": t, "output": redact_urls_in_text(t)} for t in texts]
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    urls = [
        "", "https://h:9200", "https://u:p@h:9200/x?q=1#f", "https://u@h", "user:pw@host:9200",
        "http://[::1]:9200", "http://u:p@[::1]:9200", "http://[::1", "https://a@b@c/x", "no-scheme-host",
    ]
    url_cases = [{"url": u, "redacted": redact_url(u), "has_userinfo": url_has_userinfo(u)} for u in urls]
    return {"env": env, "texts": text_cases, "urls": url_cases}


def gen_misc() -> dict:
    names = [".env", ".env.local", "env", "a.pem", "A.PEM", "server.key", "key", "id_rsa", "id_rsa.pub",
             "id_ed25519", "credentials", "credentials.json", "Credentials.json", "secrets.yaml",
             "my_secrets", "readme.md", "x.keys", "[a].pem", ".envrc"]
    secrets = [{"name": n, "secret": is_secret_filename(n)} for n in names]
    locks = []
    for owner in [None, {}, {"pid": 42, "operation": "index", "source_id": "src_1", "hostname": "box"},
                  {"pid": None, "operation": "rebuild"}, {"pid": 7}]:
        for timeout in (30.0, 0.5, 1e-07, 3600.0, 2.25):
            err = RunLockTimeoutError("/x/locks/index.lock", timeout, owner=owner)
            locks.append({"owner": owner, "timeout": timeout, "message": str(err), "exit_code": err.exit_code})
    rel = {
        "sources_db": "sources.db", "user_config": "config.yaml", "logs": "logs", "backups": "backups",
        "locks": "locks", "tmp": "tmp", "daemon_pid": "daemon.pid", "daemon_health": "daemon_health.json",
        "index_progress": "index_progress.json", "install_info": "install_info.json", "update_cache": "update.json",
        "projects": "projects",
    }
    home = Path("/H")
    check = {
        "sources_db": paths.sources_db_path(home), "user_config": paths.user_config_path(home),
        "logs": paths.logs_dir(home), "backups": paths.backups_dir(home), "locks": paths.locks_dir(home),
        "tmp": paths.tmp_dir(home), "daemon_pid": paths.daemon_pid_path(home),
        "daemon_health": paths.daemon_health_path(home), "index_progress": paths.index_progress_path(home),
        "install_info": paths.install_info_path(home), "update_cache": paths.update_cache_path(home),
        "projects": paths.projects_dir(home),
    }
    assert all(check[k] == home / v for k, v in rel.items())
    project = {
        "project_db": str(paths.project_db_path("abc", home).relative_to(home)),
        "project_cache": str(paths.project_cache_dir("abc", home).relative_to(home)),
        "project_state": str(paths.project_state_dir("abc", home).relative_to(home)),
        "vector_index": str(paths.project_vector_index_path("abc", home).relative_to(home)),
        "vector_meta": str(paths.project_vector_meta_path("abc", home).relative_to(home)),
    }
    return {"secrets": secrets, "lock_timeouts": locks, "home_paths": rel, "project_paths": project}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("core_config.json", gen_config()),
        ("core_ids.json", gen_ids()),
        ("core_redaction.json", gen_redaction()),
        ("core_misc.json", gen_misc()),
    ):
        text = json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
        (OUT / name).write_text(text, encoding="utf-8")
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
