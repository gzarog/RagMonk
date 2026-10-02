//! The typed configuration schema (`ragmonk.core.config` models).
//!
//! Field order is significant: it drives validation-error order and the
//! `config show` / `config.yaml` output order, both matching pydantic.

use crate::coerce::{self, Coerced};
use crate::pyvalue::{py_repr_str, py_repr_str_list, PyValue};

/// The pinned embedding model's maximum sequence length
/// (`ragmonk.tokenization.model_identity.MAX_SEQUENCE_TOKENS`).
pub const MAX_SEQUENCE_TOKENS: i64 = 256;

/// Collected `(loc, msg)` validation errors, in pydantic order.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Errors(pub Vec<(String, String)>);

impl Errors {
    pub fn push(&mut self, loc: &str, msg: impl Into<String>) {
        self.0.push((loc.to_owned(), msg.into()));
    }
    pub fn len(&self) -> usize {
        self.0.len()
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
    /// pydantic's `"; ".join(f"{loc}: {msg}")` rendering.
    pub fn render(&self) -> String {
        self.0
            .iter()
            .map(|(loc, msg)| {
                if loc.is_empty() {
                    msg.clone()
                } else {
                    format!("{loc}: {msg}")
                }
            })
            .collect::<Vec<_>>()
            .join("; ")
    }
}

pub fn join_loc(parent: &str, child: &str) -> String {
    if parent.is_empty() {
        child.to_owned()
    } else {
        format!("{parent}.{child}")
    }
}

/// A field type: lax coercion from a Python value, and `model_dump(mode="json")`.
pub trait ConfigField: Sized + Clone {
    fn coerce(errors: &mut Errors, value: &PyValue, loc: &str) -> Option<Self>;
    fn dump(&self) -> PyValue;
}

fn scalar<T>(errors: &mut Errors, loc: &str, r: Coerced<T>) -> Option<T> {
    r.map_err(|msg| errors.push(loc, msg)).ok()
}

impl ConfigField for i64 {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        scalar(e, loc, coerce::to_int(v))
    }
    fn dump(&self) -> PyValue {
        PyValue::Int(i128::from(*self))
    }
}

impl ConfigField for f64 {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        scalar(e, loc, coerce::to_float(v))
    }
    fn dump(&self) -> PyValue {
        PyValue::Float(*self)
    }
}

impl ConfigField for bool {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        scalar(e, loc, coerce::to_bool(v))
    }
    fn dump(&self) -> PyValue {
        PyValue::Bool(*self)
    }
}

impl ConfigField for String {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        scalar(e, loc, coerce::to_str(v))
    }
    fn dump(&self) -> PyValue {
        PyValue::Str(self.clone())
    }
}

impl ConfigField for Option<String> {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        scalar(e, loc, coerce::to_opt_str(v))
    }
    fn dump(&self) -> PyValue {
        self.as_ref()
            .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
    }
}

impl ConfigField for Vec<String> {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        let PyValue::List(items) = v else {
            e.push(loc, coerce::MSG_LIST_TYPE);
            return None;
        };
        let start = e.len();
        let out: Vec<Option<String>> = items
            .iter()
            .enumerate()
            .map(|(i, item)| scalar(e, &join_loc(loc, &i.to_string()), coerce::to_str(item)))
            .collect();
        (e.len() == start).then(|| out.into_iter().flatten().collect())
    }
    fn dump(&self) -> PyValue {
        PyValue::List(self.iter().map(|s| PyValue::Str(s.clone())).collect())
    }
}

/// `documents.chunking.max_tokens: int | Literal["auto"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MaxTokens {
    Auto,
    Value(i64),
}

impl ConfigField for MaxTokens {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        if matches!(v, PyValue::Str(s) if s == "auto") {
            return Some(MaxTokens::Auto);
        }
        match coerce::to_int(v) {
            Ok(i) => Some(MaxTokens::Value(i)),
            Err(msg) => {
                e.push(&format!("{loc}.int"), msg);
                e.push(&format!("{loc}.literal['auto']"), "Input should be 'auto'");
                None
            }
        }
    }
    fn dump(&self) -> PyValue {
        match self {
            MaxTokens::Auto => PyValue::str("auto"),
            MaxTokens::Value(i) => PyValue::Int(i128::from(*i)),
        }
    }
}

/// `storage.server.engine: Literal["opensearch", "elasticsearch"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageEngine {
    OpenSearch,
    Elasticsearch,
}

impl StorageEngine {
    pub fn as_str(self) -> &'static str {
        match self {
            StorageEngine::OpenSearch => "opensearch",
            StorageEngine::Elasticsearch => "elasticsearch",
        }
    }
}

impl ConfigField for StorageEngine {
    fn coerce(e: &mut Errors, v: &PyValue, loc: &str) -> Option<Self> {
        match v {
            PyValue::Str(s) if s == "opensearch" => Some(StorageEngine::OpenSearch),
            PyValue::Str(s) if s == "elasticsearch" => Some(StorageEngine::Elasticsearch),
            _ => {
                e.push(loc, "Input should be 'opensearch' or 'elasticsearch'");
                None
            }
        }
    }
    fn dump(&self) -> PyValue {
        PyValue::str(self.as_str())
    }
}

type Check<T> = fn(&T) -> Result<(), String>;

fn coerce_field<T: ConfigField>(
    e: &mut Errors,
    obj: &PyValue,
    loc: &str,
    key: &str,
    default: &T,
    check: Option<Check<T>>,
) -> Option<T> {
    let floc = join_loc(loc, key);
    let value = match obj.get(key) {
        None => return Some(default.clone()),
        Some(v) => T::coerce(e, v, &floc)?,
    };
    if let Some(check) = check {
        if let Err(msg) = check(&value) {
            e.push(&floc, format!("Value error, {msg}"));
            return None;
        }
    }
    Some(value)
}

macro_rules! section {
    (
        $(#[$meta:meta])*
        $name:ident {
            $( $field:ident : $ty:ty = $default:expr $(, check = $check:expr)? ; )*
        }
        $(model_check = $mcheck:expr ;)?
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq)]
        pub struct $name {
            $( pub $field: $ty, )*
        }

        impl Default for $name {
            fn default() -> Self {
                Self { $( $field: $default, )* }
            }
        }

        impl ConfigField for $name {
            fn coerce(e: &mut Errors, value: &PyValue, loc: &str) -> Option<Self> {
                if value.as_dict().is_none() {
                    e.push(loc, concat!(
                        "Input should be a valid dictionary or instance of ",
                        stringify!($name)
                    ));
                    return None;
                }
                let defaults = Self::default();
                let start = e.len();
                $(
                    #[allow(unused_mut, unused_assignments)]
                    let mut check: Option<Check<$ty>> = None;
                    $( check = Some($check); )?
                    let $field = coerce_field::<$ty>(
                        e, value, loc, stringify!($field), &defaults.$field, check,
                    );
                )*
                if e.len() != start {
                    return None;
                }
                let out = Self { $( $field: $field?, )* };
                $(
                    let mcheck: Check<Self> = $mcheck;
                    if let Err(msg) = mcheck(&out) {
                        e.push(loc, format!("Value error, {msg}"));
                        return None;
                    }
                )?
                Some(out)
            }

            fn dump(&self) -> PyValue {
                PyValue::Dict(vec![
                    $( (PyValue::str(stringify!($field)), self.$field.dump()), )*
                ])
            }
        }
    };
}

fn one_of(field: &str, value: &str, allowed: &[&str]) -> Result<(), String> {
    if allowed.contains(&value) {
        return Ok(());
    }
    let mut sorted = allowed.to_vec();
    sorted.sort_unstable();
    Err(format!(
        "unknown {field} {}; expected one of {}",
        py_repr_str(value),
        py_repr_str_list(&sorted)
    ))
}

section! {
    RuntimeConfig {
        log_level: String = "info".into();
        max_workers: i64 = 6;
        max_memory_mb: i64 = 4096;
        temp_directory: String = "auto".into();
        sqlite_cache_size_mb: i64 = 64;
    }
}

section! {
    IndexingConfig {
        watch: bool = true;
        debounce_ms: i64 = 2000;
        max_file_size_mb: i64 = 100;
        follow_symlinks: bool = false;
        hash_algorithm: String = "sha256".into();
        network_poll_seconds: i64 = 30;
        reconciliation_interval_seconds: i64 = 900;
        code_extraction_workers: i64 = 1;
        document_extraction_workers: i64 = 1;
        embedding_batch_size: i64 = 16;
        lock_timeout_seconds: f64 = 30.0, check = |v| {
            if *v > 0.0 && *v <= 3600.0 { Ok(()) } else {
                Err("indexing.lock_timeout_seconds must be > 0 and <= 3600".into())
            }
        };
        status_stall_threshold_seconds: f64 = 120.0, check = |v| {
            if *v <= 0.0 { Err("indexing.status_stall_threshold_seconds must be > 0".into()) } else { Ok(()) }
        };
    }
}

impl ChunkingConfig {
    /// `"auto"` resolves to the pinned model's `MAX_SEQUENCE_TOKENS`.
    pub fn resolved_max_tokens(&self) -> i64 {
        match self.max_tokens {
            MaxTokens::Auto => MAX_SEQUENCE_TOKENS,
            MaxTokens::Value(v) => v,
        }
    }
}

section! {
    ChunkingConfig {
        strategy: String = "hybrid".into(), check = |v| one_of("documents.chunking.strategy", v, &["hybrid"]);
        max_tokens: MaxTokens = MaxTokens::Auto, check = |v| match v {
            MaxTokens::Auto => Ok(()),
            MaxTokens::Value(n) if *n < 16 => Err("documents.chunking.max_tokens must be at least 16".into()),
            MaxTokens::Value(n) if *n > MAX_SEQUENCE_TOKENS => Err(format!(
                "documents.chunking.max_tokens ({n}) exceeds the embedding model's \
                 maximum sequence length ({MAX_SEQUENCE_TOKENS}); a larger value would \
                 let chunks be silently truncated at embedding time. Use 'auto' or a \
                 value <= {MAX_SEQUENCE_TOKENS}."
            )),
            MaxTokens::Value(_) => Ok(()),
        };
        min_tokens: i64 = 60, check = |v| if *v < 1 { Err("documents.chunking.min_tokens must be at least 1".into()) } else { Ok(()) };
        overlap_tokens: i64 = 40, check = |v| if *v < 0 { Err("documents.chunking.overlap_tokens must not be negative".into()) } else { Ok(()) };
        safety_tokens: i64 = 4, check = |v| if *v < 0 { Err("documents.chunking.safety_tokens must not be negative".into()) } else { Ok(()) };
        merge_peers: bool = true;
    }
    model_check = |c| {
        let resolved = c.resolved_max_tokens();
        if c.min_tokens > resolved {
            return Err("documents.chunking.min_tokens must not exceed documents.chunking.max_tokens".into());
        }
        if c.overlap_tokens >= resolved {
            return Err("documents.chunking.overlap_tokens must be less than documents.chunking.max_tokens".into());
        }
        if c.safety_tokens >= resolved {
            return Err("documents.chunking.safety_tokens must be less than documents.chunking.max_tokens".into());
        }
        Ok(())
    };
}

fn positive_attachment(field: &'static str) -> Check<i64> {
    match field {
        "email_attachment_max_bytes" => |v| {
            if *v <= 0 {
                Err("documents.email_attachment_max_bytes must be > 0".into())
            } else {
                Ok(())
            }
        },
        "email_attachment_max_count" => |v| {
            if *v <= 0 {
                Err("documents.email_attachment_max_count must be > 0".into())
            } else {
                Ok(())
            }
        },
        _ => |v| {
            if *v <= 0 {
                Err("documents.email_attachment_total_max_bytes must be > 0".into())
            } else {
                Ok(())
            }
        },
    }
}

section! {
    DocumentsConfig {
        enabled: bool = true;
        ocr: String = "auto".into(), check = |v| one_of("documents.ocr", v, &["off", "auto", "always"]);
        max_pages: i64 = 1000;
        chunking: ChunkingConfig = ChunkingConfig::default();
        image_ocr: bool = false;
        pdf_mode: String = "accurate".into(), check = |v| one_of("documents.pdf_mode", v, &["accurate", "fast"]);
        pdf_table_structure: bool = true;
        pdf_process_workers: i64 = 1, check = |v| {
            if (1..=32).contains(v) { Ok(()) } else { Err("documents.pdf_process_workers must be between 1 and 32".into()) }
        };
        email_attachments: bool = true;
        email_attachment_max_bytes: i64 = 25 * 1024 * 1024, check = positive_attachment("email_attachment_max_bytes");
        email_attachment_max_count: i64 = 50, check = positive_attachment("email_attachment_max_count");
        email_attachment_total_max_bytes: i64 = 100 * 1024 * 1024, check = positive_attachment("email_attachment_total_max_bytes");
    }
}

section! {
    CodeConfig {
        enabled: bool = true;
    }
}

section! {
    SearchVectorConfig {
        engine: String = "auto".into();
        rebuild_deleted_ratio: f64 = 0.15;
    }
}

section! {
    SearchCacheConfig {
        enabled: bool = true;
        max_queries: i64 = 256;
        max_query_embeddings: i64 = 256;
    }
}

section! {
    SearchOutputConfig {
        fallback: Vec<String> = vec!["snippets".into(), "json".into(), "files".into()], check = |v| {
            if v.is_empty() {
                return Err("search.output.fallback must not be empty".into());
            }
            for mode in v {
                if !["snippets", "json", "files", "table"].contains(&mode.as_str()) {
                    return Err(format!(
                        "unknown search.output.fallback mode {}; expected one of ['files', 'json', 'snippets', 'table']",
                        py_repr_str(mode)
                    ));
                }
            }
            Ok(())
        };
        snippet_max_tokens: i64 = 32, check = |v| if (1..=64).contains(v) { Ok(()) } else {
            Err("search.output.snippet_max_tokens must be between 1 and 64 (SQLite FTS5's own snippet() limit)".into())
        };
    }
}

fn non_negative_chunks(v: &i64) -> Result<(), String> {
    if *v < 0 {
        Err("search.context.previous_chunks/next_chunks must be >= 0".into())
    } else {
        Ok(())
    }
}

section! {
    SearchContextConfig {
        parent_heading: bool = true;
        previous_chunks: i64 = 1, check = non_negative_chunks;
        next_chunks: i64 = 1, check = non_negative_chunks;
        max_tokens: i64 = 1200, check = |v| if *v <= 0 { Err("search.context.max_tokens must be > 0".into()) } else { Ok(()) };
    }
}

section! {
    SearchRerankerConfig {
        enabled: bool = false;
        top_n: i64 = 20, check = |v| if *v < 1 { Err("search.reranker.top_n must be >= 1".into()) } else { Ok(()) };
    }
}

section! {
    SearchConfig {
        lexical: bool = true;
        graph: bool = true;
        semantic: bool = false;
        lazy_semantic: bool = false;
        semantic_top_k: i64 = 30;
        vector: SearchVectorConfig = SearchVectorConfig::default();
        cache: SearchCacheConfig = SearchCacheConfig::default();
        output: SearchOutputConfig = SearchOutputConfig::default();
        context: SearchContextConfig = SearchContextConfig::default();
        reranker: SearchRerankerConfig = SearchRerankerConfig::default();
    }
}

section! {
    ContextConfig {
        max_chars: i64 = 30000;
        max_files: i64 = 20;
        max_graph_nodes: i64 = 100;
    }
}

section! {
    McpConfig {
        enabled: bool = true;
        request_timeout_seconds: f64 = 30.0;
    }
}

section! {
    ApiConfig {
        enabled: bool = false;
        bind: String = "127.0.0.1".into();
        port: i64 = 8765;
    }
}

section! {
    PrivacyConfig {
        external_ai_allowed: bool = false;
    }
}

section! {
    /// Stores no credential; sign-in is delegated to the Codex runtime.
    CodexAiConfig {
        auth_mode: String = "chatgpt".into(), check = |v| one_of("ai.codex.auth_mode", v, &["chatgpt"]);
    }
}

section! {
    /// Stores no credential; relies on the signed-in Copilot CLI.
    GithubCopilotAiConfig {
        auth_mode: String = "signed_in_user".into(), check = |v| one_of("ai.github_copilot.auth_mode", v, &["signed_in_user"]);
    }
}

section! {
    /// API keys are never stored here; providers read them from env vars.
    AiConfig {
        provider: String = "none".into();
        model: String = String::new();
        base_url: Option<String> = None;
        timeout_seconds: f64 = 60.0;
        codex: CodexAiConfig = CodexAiConfig::default();
        github_copilot: GithubCopilotAiConfig = GithubCopilotAiConfig::default();
    }
}

section! {
    TelemetryConfig {
        anonymous_usage: bool = false;
    }
}

section! {
    UpdatesConfig {
        enabled: bool = true;
        check_interval_hours: i64 = 24;
        notify: bool = true;
        channel: String = "stable".into();
    }
}

fn bulk_positive(v: &i64) -> Result<(), String> {
    if *v < 1 {
        Err("storage.server.bulk fields must be >= 1".into())
    } else {
        Ok(())
    }
}

section! {
    BulkConfig {
        max_actions: i64 = 500, check = bulk_positive;
        max_bytes: i64 = 5_000_000, check = bulk_positive;
        concurrency: i64 = 2, check = bulk_positive;
        max_retries: i64 = 3, check = |v| if *v < 0 { Err("storage.server.bulk.max_retries must be >= 0".into()) } else { Ok(()) };
    }
}

section! {
    /// Never holds credentials: they come from
    /// `RAGMONK_{OPENSEARCH,ELASTICSEARCH}_{USERNAME,PASSWORD,API_KEY}`.
    ServerStorageConfig {
        engine: StorageEngine = StorageEngine::OpenSearch;
        url: String = String::new(), check = |v| {
            if ragmonk_telemetry::redact::url_has_userinfo(v) {
                Err("storage.server.url must not contain credentials (user-info such as \
                     'user:password@'); set RAGMONK_OPENSEARCH_USERNAME/_PASSWORD/_API_KEY \
                     or RAGMONK_ELASTICSEARCH_USERNAME/_PASSWORD/_API_KEY instead".into())
            } else {
                Ok(())
            }
        };
        index_prefix: String = "ragmonk".into();
        verify_tls: bool = true;
        request_timeout_seconds: f64 = 30.0, check = |v| {
            if *v <= 0.0 { Err("storage.server.request_timeout_seconds must be > 0".into()) } else { Ok(()) }
        };
        bulk: BulkConfig = BulkConfig::default();
    }
}

section! {
    /// `mode="local"` stays the default even when the section is absent.
    StorageConfig {
        mode: String = "local".into(), check = |v| one_of("storage.mode", v, &["local", "server"]);
        server: ServerStorageConfig = ServerStorageConfig::default();
    }
}

section! {
    RagMonkConfig {
        version: i64 = 1;
        runtime: RuntimeConfig = RuntimeConfig::default();
        indexing: IndexingConfig = IndexingConfig::default();
        documents: DocumentsConfig = DocumentsConfig::default();
        code: CodeConfig = CodeConfig::default();
        search: SearchConfig = SearchConfig::default();
        context: ContextConfig = ContextConfig::default();
        mcp: McpConfig = McpConfig::default();
        api: ApiConfig = ApiConfig::default();
        privacy: PrivacyConfig = PrivacyConfig::default();
        telemetry: TelemetryConfig = TelemetryConfig::default();
        ai: AiConfig = AiConfig::default();
        updates: UpdatesConfig = UpdatesConfig::default();
        storage: StorageConfig = StorageConfig::default();
    }
}

/// Top-level sections recognized by the `RAGMONK_<SECTION>__...` env layer.
pub const KNOWN_SECTIONS: &[&str] = &[
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
    "storage",
];

impl RagMonkConfig {
    /// `RagMonkConfig.model_validate(value)`.
    pub fn validate(value: &PyValue) -> Result<Self, Errors> {
        let mut errors = Errors::default();
        match <Self as ConfigField>::coerce(&mut errors, value, "") {
            Some(cfg) if errors.is_empty() => Ok(cfg),
            _ => Err(errors),
        }
    }

    /// `model_dump(mode="json")`.
    pub fn to_pyvalue(&self) -> PyValue {
        self.dump()
    }
}
