//! JSON-lines logging compatible with `ragmonk.telemetry.logging`.
//!
//! Each record is `{"timestamp": ..., "level": ..., "component": ...,
//! "event": ..., <extra fields>}` serialized like Python's `json.dumps`
//! (`", "`/`": "` separators, ASCII-escaped). Emit records with
//! `tracing` events carrying `component` and `event` fields:
//!
//! ```ignore
//! tracing::info!(component = "indexing", event = "file_indexed", path = %p);
//! ```
//!
//! Only paths, ids and durations are logged, never file contents. As a
//! hardening over the reference, string values are additionally passed
//! through [`crate::redact::redact_urls_in_text`].

use std::fmt::Write as _;
use std::fs::{File, OpenOptions};
use std::io::Write as _;
use std::path::Path;
use std::sync::Mutex;

use chrono::{DateTime, Timelike, Utc};
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::Layer;

use crate::redact::redact_urls_in_text;

/// One structured field value.
#[derive(Debug, Clone, PartialEq)]
pub enum FieldValue {
    Str(String),
    Int(i128),
    Float(f64),
    Bool(bool),
}

/// A formatted log record before serialization.
#[derive(Debug, Clone, PartialEq)]
pub struct Record {
    pub timestamp: DateTime<Utc>,
    pub level: &'static str,
    pub component: String,
    pub event: String,
    pub fields: Vec<(String, FieldValue)>,
}

/// Python logging level name for a tracing level.
pub fn level_name(level: &Level) -> &'static str {
    match *level {
        Level::ERROR => "ERROR",
        Level::WARN => "WARNING",
        Level::INFO => "INFO",
        Level::DEBUG | Level::TRACE => "DEBUG",
    }
}

/// `datetime.isoformat()` of an aware UTC datetime.
pub fn python_isoformat(ts: &DateTime<Utc>) -> String {
    let base = ts.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = ts.nanosecond() / 1_000 % 1_000_000;
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

/// `json.dumps(str)` with the default `ensure_ascii=True`.
pub fn python_json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python `float.__repr__` (shortest round-trip, `NaN`/`Infinity` as json.dumps emits).
pub fn python_float_json(v: f64) -> String {
    if v.is_nan() {
        return "NaN".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    python_float_repr(v)
}

/// Python `repr(float)` for finite values.
pub fn python_float_repr(v: f64) -> String {
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    // Rust's `{:e}` gives the shortest round-trip digits.
    let sci = format!("{v:e}");
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let point = exp + 1;
        let s = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!(
                "{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        };
        format!("{sign}{s}")
    } else {
        let mut m = digits[..1].to_owned();
        if digits.len() > 1 {
            m.push('.');
            m.push_str(&digits[1..]);
        }
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{m}e{esign}{:02}", exp.abs())
    }
}

impl Record {
    pub fn to_json_line(&self) -> String {
        let mut out = String::from("{");
        let mut first = true;
        let mut key = |out: &mut String, k: &str| {
            if !first {
                out.push_str(", ");
            }
            first = false;
            python_json_str(k, out);
            out.push_str(": ");
        };
        key(&mut out, "timestamp");
        python_json_str(&python_isoformat(&self.timestamp), &mut out);
        key(&mut out, "level");
        python_json_str(self.level, &mut out);
        key(&mut out, "component");
        python_json_str(&self.component, &mut out);
        key(&mut out, "event");
        python_json_str(&self.event, &mut out);
        for (k, v) in &self.fields {
            if matches!(k.as_str(), "timestamp" | "level" | "component" | "event") {
                continue;
            }
            key(&mut out, k);
            match v {
                FieldValue::Str(s) => python_json_str(s, &mut out),
                FieldValue::Int(i) => {
                    let _ = write!(out, "{i}");
                }
                FieldValue::Float(f) => out.push_str(&python_float_json(*f)),
                FieldValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            }
        }
        out.push('}');
        out
    }
}

#[derive(Default)]
struct Collector {
    component: Option<String>,
    event: Option<String>,
    message: Option<String>,
    fields: Vec<(String, FieldValue)>,
}

impl Collector {
    fn put(&mut self, field: &Field, value: FieldValue) {
        let name = field.name();
        let text = || match &value {
            FieldValue::Str(s) => s.clone(),
            FieldValue::Int(i) => i.to_string(),
            FieldValue::Float(f) => python_float_repr(*f),
            FieldValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        };
        match name {
            "component" => self.component = Some(text()),
            "event" => self.event = Some(text()),
            "message" => self.message = Some(text()),
            _ => self.fields.push((name.to_owned(), value)),
        }
    }
}

impl Visit for Collector {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.put(field, FieldValue::Str(redact_urls_in_text(value)));
    }
    fn record_i64(&mut self, field: &Field, value: i64) {
        self.put(field, FieldValue::Int(value.into()));
    }
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.put(field, FieldValue::Int(value.into()));
    }
    fn record_i128(&mut self, field: &Field, value: i128) {
        self.put(field, FieldValue::Int(value));
    }
    fn record_f64(&mut self, field: &Field, value: f64) {
        self.put(field, FieldValue::Float(value));
    }
    fn record_bool(&mut self, field: &Field, value: bool) {
        self.put(field, FieldValue::Bool(value));
    }
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.put(
            field,
            FieldValue::Str(redact_urls_in_text(&format!("{value:?}"))),
        );
    }
}

/// Builds a [`Record`] from a tracing event.
pub fn record_from_event(event: &Event<'_>, now: DateTime<Utc>) -> Record {
    let mut c = Collector::default();
    event.record(&mut c);
    let meta = event.metadata();
    let event_name = c.event.or(c.message).unwrap_or_default();
    Record {
        timestamp: now,
        level: level_name(meta.level()),
        component: c.component.unwrap_or_else(|| meta.target().to_owned()),
        event: event_name,
        fields: c.fields,
    }
}

/// Appends JSON records to a file.
pub struct JsonFileLayer {
    file: Mutex<File>,
    max_level: Level,
}

impl JsonFileLayer {
    pub fn open(logs_dir: &Path, max_level: Level) -> std::io::Result<Self> {
        std::fs::create_dir_all(logs_dir)?;
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(logs_dir.join("ragmonk.log"))?;
        Ok(Self {
            file: Mutex::new(file),
            max_level,
        })
    }
}

impl<S: Subscriber> Layer<S> for JsonFileLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        if *event.metadata().level() > self.max_level {
            return;
        }
        let mut line = record_from_event(event, Utc::now()).to_json_line();
        line.push('\n');
        if let Ok(mut file) = self.file.lock() {
            let _ = file.write_all(line.as_bytes());
        }
    }
}

/// Console output: WARNING and above only, like the reference.
pub struct ConsoleLayer {
    json: bool,
}

impl<S: Subscriber> Layer<S> for ConsoleLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        if *event.metadata().level() > Level::WARN {
            return;
        }
        let record = record_from_event(event, Utc::now());
        let line = if self.json {
            record.to_json_line()
        } else {
            format!("{:<8} {}: {}", record.level, record.component, record.event)
        };
        eprintln!("{line}");
    }
}

/// Python level string (`info`, `warning`, ...) to a tracing level.
pub fn parse_level(level: &str) -> Option<Level> {
    match level.trim().to_ascii_uppercase().as_str() {
        "DEBUG" | "NOTSET" => Some(Level::DEBUG),
        "INFO" => Some(Level::INFO),
        "WARNING" | "WARN" => Some(Level::WARN),
        "ERROR" | "CRITICAL" | "FATAL" => Some(Level::ERROR),
        _ => None,
    }
}

/// `configure_logging`: installs the global subscriber. Returns an error
/// for an unknown level (Python raises `ValueError` from `setLevel`).
pub fn configure_logging(
    logs_dir: &Path,
    level: &str,
    console_format: &str,
) -> std::io::Result<()> {
    let level = parse_level(level).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("Unknown level: {level:?}"),
        )
    })?;
    let subscriber = tracing_subscriber::registry()
        .with(JsonFileLayer::open(logs_dir, level)?)
        .with(ConsoleLayer {
            json: console_format == "json",
        });
    tracing::subscriber::set_global_default(subscriber)
        .map_err(|e| std::io::Error::other(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn isoformat_matches_python() {
        let t = Utc.with_ymd_and_hms(2026, 10, 2, 16, 30, 6).unwrap();
        assert_eq!(python_isoformat(&t), "2026-10-02T16:30:06+00:00");
        let t = t + chrono::Duration::microseconds(463_119);
        assert_eq!(python_isoformat(&t), "2026-10-02T16:30:06.463119+00:00");
    }

    #[test]
    fn float_repr_matches_python() {
        for (v, s) in [
            (30.0, "30.0"),
            (0.1, "0.1"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (-2.5, "-2.5"),
            (123.456, "123.456"),
            (1.2345678901234567e19, "1.2345678901234567e+19"),
        ] {
            assert_eq!(python_float_repr(v), s, "{v}");
        }
    }

    #[test]
    fn json_line_matches_python_shape() {
        let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
        let r = Record {
            timestamp: t,
            level: "INFO",
            component: "indexing".into(),
            event: "file_indexed".into(),
            fields: vec![
                ("path".into(), FieldValue::Str("/a/é".into())),
                ("ms".into(), FieldValue::Float(1.5)),
                ("n".into(), FieldValue::Int(3)),
                ("ok".into(), FieldValue::Bool(true)),
            ],
        };
        assert_eq!(
            r.to_json_line(),
            r#"{"timestamp": "2026-01-01T00:00:00+00:00", "level": "INFO", "component": "indexing", "event": "file_indexed", "path": "/a/\u00e9", "ms": 1.5, "n": 3, "ok": true}"#
        );
    }

    #[test]
    fn layer_writes_json_records_and_redacts() {
        let dir = tempfile::tempdir().unwrap();
        let layer = JsonFileLayer::open(dir.path(), Level::INFO).unwrap();
        let subscriber = tracing_subscriber::registry().with(layer);
        tracing::subscriber::with_default(subscriber, || {
            tracing::info!(
                component = "backend",
                event = "connect",
                url = "https://u:p@h:9200"
            );
            tracing::debug!(component = "x", event = "dropped");
        });
        let text = std::fs::read_to_string(dir.path().join("ragmonk.log")).unwrap();
        let lines: Vec<_> = text.lines().collect();
        assert_eq!(lines.len(), 1);
        let v: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v["component"], "backend");
        assert_eq!(v["event"], "connect");
        assert_eq!(v["level"], "INFO");
        assert_eq!(v["url"], "https://h:9200");
    }
}
