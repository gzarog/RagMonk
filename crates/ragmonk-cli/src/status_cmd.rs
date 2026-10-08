//! `ragmonk status [--json] [--watch] [--interval S] [--errors] [--verbose]`
//! `--json` is the full status model. The text view renders its panels
//! and sources table as plain text.

use std::time::{Duration, Instant};

use ragmonk_core::errors::RagMonkError;
use serde_json::Value;

use ragmonk_service::status::collect;

use crate::workflow::print_table;
use crate::{prepared_home, print_json};

#[derive(clap::Args)]
pub struct StatusArgs {
    /// Machine-readable output (full status model).
    #[arg(long = "json")]
    json: bool,
    /// Refresh continuously with deltas; Ctrl+C to exit.
    #[arg(long)]
    watch: bool,
    /// Seconds between --watch refreshes.
    #[arg(long, default_value_t = 2.0)]
    interval: f64,
    /// Only problematic sources and recent errors.
    #[arg(long)]
    errors: bool,
    /// Lock, progress, queue and error details.
    #[arg(long, short = 'v')]
    verbose: bool,
}

fn fmt_age(seconds: Option<f64>) -> String {
    match seconds {
        None => "-".into(),
        Some(s) if s < 60.0 => format!("{s:.0}s ago"),
        Some(s) if s < 3600.0 => format!("{:.0}m ago", s / 60.0),
        Some(s) if s < 86400.0 => format!("{:.1}h ago", s / 3600.0),
        Some(s) => format!("{:.1}d ago", s / 86400.0),
    }
}

fn fmt_duration(seconds: Option<f64>) -> String {
    let Some(s) = seconds else {
        return "-".into();
    };
    let s = s as i64;
    let (minutes, secs) = (s / 60, s % 60);
    let (hours, minutes) = (minutes / 60, minutes % 60);
    if hours > 0 {
        format!("{hours}h{minutes:02}m")
    } else {
        format!("{minutes}m{secs:02}s")
    }
}

fn fmt_since(iso: Option<&str>) -> String {
    let Some(iso) = iso.filter(|s| !s.is_empty()) else {
        return "-".into();
    };
    match chrono::DateTime::parse_from_rfc3339(iso) {
        Ok(t) => fmt_age(Some(
            (chrono::Utc::now() - t.with_timezone(&chrono::Utc))
                .as_seconds_f64()
                .max(0.0),
        )),
        Err(_) => iso.into(),
    }
}

fn s(v: &Value) -> String {
    match v {
        Value::Null => "-".into(),
        Value::String(x) if x.is_empty() => "-".into(),
        Value::String(x) => x.clone(),
        other => other.to_string(),
    }
}

fn i(v: &Value) -> i64 {
    v.as_i64().unwrap_or(0)
}

/// Presentation-only deltas between two `--watch` samples.
#[derive(Clone, Copy)]
struct Sample {
    at: Instant,
    indexed: i64,
    failed: i64,
    retry: i64,
    depth: i64,
}

fn sample(d: &Value) -> Sample {
    Sample {
        at: Instant::now(),
        indexed: i(&d["totals"]["by_status"]["indexed"]),
        failed: i(&d["totals"]["by_status"]["failed"]),
        retry: i(&d["queue"]["retry"]),
        depth: i(&d["queue"]["depth"]),
    }
}

fn signed(v: Option<i64>) -> String {
    v.map_or_else(|| "n/a".into(), |n| format!("{n:+}"))
}

fn panel(title: &str, body: &str) -> String {
    format!("== {title} ==\n{body}\n")
}

/// The text view (`render_status`).
fn render(
    d: &Value,
    errors_only: bool,
    verbose: bool,
    deltas: Option<(Option<Sample>, Sample)>,
) -> String {
    let mut out = String::new();
    let ix = &d["indexer"];
    let state = ix["state"].as_str().unwrap_or("-");
    let mut b = format!("State: {state}");
    if let Some(r) = ix["stall_reason"].as_str() {
        b += &format!("  ({r})");
    }
    if matches!(state, "running" | "stalled") {
        b += &format!(
            "\nPID: {}  Operation: {}  Running for: {}",
            s(&ix["pid"]),
            s(&ix["operation"]),
            fmt_duration(ix["running_for_seconds"].as_f64())
        );
        let where_ = match (ix["source_position"].as_i64(), ix["source_total"].as_i64()) {
            (Some(p), Some(t)) if p > 0 && t > 0 => format!(" ({p}/{t})"),
            _ => String::new(),
        };
        b += &format!(
            "\nSource: {}{where_}  Stage: {}  Last activity: {}",
            s(&ix["current_source_id"]),
            s(&ix["stage"]),
            fmt_age(ix["last_activity_age_seconds"].as_f64())
        );
        let run = &ix["run"];
        if run.is_object() {
            b += &format!(
                "\nThis run: scanned={} indexed={} retry={} failed={}",
                i(&run["scanned"]),
                i(&run["indexed"]),
                i(&run["retry"]),
                i(&run["failed"])
            );
        }
    }
    let last = &ix["last_run"];
    if last.is_object() && !matches!(state, "running" | "stalled") {
        let at = if last["completed_at"].is_string() {
            &last["completed_at"]
        } else {
            &last["updated_at"]
        };
        b += &format!(
            "\nLast run: {} {} at {}",
            s(&last["operation"]),
            s(&last["outcome"]),
            s(at)
        );
        if let Some(e) = last["error"].as_str() {
            b += &format!("\n  {e}");
        }
    }
    if verbose {
        b += &format!(
            "\nLock: {}  owner pid={} host={} source={} acquired={}",
            s(&ix["lock_state"]),
            s(&ix["pid"]),
            s(&ix["hostname"]),
            s(&ix["lock_source_id"]),
            s(&ix["acquired_at"])
        );
        b += &format!(
            "\nProgress: started={} last_activity={} stall_threshold={}s",
            s(&ix["started_at"]),
            s(&ix["last_activity_at"]),
            s(&ix["stall_threshold_seconds"])
        );
    }
    out += &panel("Indexer", &b);

    let q = &d["queue"];
    let m = &d["totals"]["metrics"];
    let mut b = format!(
        "Health: {}  ({} problem(s))",
        s(&d["health"]["status"]),
        i(&d["health"]["problem_count"])
    );
    b += &format!(
        "\nFiles: indexed={} failed={} discovered={}",
        i(&m["files_indexed"]),
        i(&m["files_failed"]),
        i(&m["files_discovered"])
    );
    b += &format!(
        "\nJobs: queued={} processing={} retry={} failed={}",
        i(&q["queued"]),
        i(&q["processing"]),
        i(&q["retry"]),
        i(&q["failed"])
    );
    if let Some((prev, cur)) = deltas {
        let (di, df, dr, dd, rate) = match prev {
            None => (None, None, None, None, None),
            Some(p) => {
                let elapsed = cur.at.duration_since(p.at).as_secs_f64();
                let di = cur.indexed - p.indexed;
                let rate = (elapsed > 0.0 && di >= 0).then(|| di as f64 / elapsed);
                (
                    Some(di),
                    Some(cur.failed - p.failed),
                    Some(cur.retry - p.retry),
                    Some(cur.depth - p.depth),
                    rate,
                )
            }
        };
        b += &format!(
            "\nDelta: indexed={} failed={} retry={} queue={}  Throughput: {}",
            signed(di),
            signed(df),
            signed(dr),
            signed(dd),
            rate.map_or_else(|| "n/a".into(), |r| format!("{r:.1} files/s"))
        );
    }
    if verbose {
        b += &format!(
            "\nOldest pending: {}  Next retry: {}  Max attempts: {}",
            s(&q["oldest_pending_created_at"]),
            s(&q["next_retry_at"]),
            i(&q["max_attempt_count"])
        );
        b += &format!(
            "\nSymbols: {}  Relationships: {}  Documents: {}  DB size: {:.1} KB",
            i(&m["symbols_created"]),
            i(&m["relationships_created"]),
            i(&m["documents_processed"]),
            i(&m["database_size_bytes"]) as f64 / 1024.0
        );
    }
    let backend = &d["backend"];
    b += &format!("\nBackend: {}", backend["type"].as_str().unwrap_or("local"));
    if let Some(e) = backend["error"].as_str() {
        b += &format!("  {e}");
    }
    out += &panel("Summary", &b);

    let mut rows: Vec<&Value> = d["sources"].as_array().into_iter().flatten().collect();
    if errors_only {
        rows.retain(|r| {
            matches!(
                r["index_state"].as_str(),
                Some("errors" | "retrying" | "offline" | "stalled")
            ) || r["last_error"].is_string()
        });
    }
    if !rows.is_empty() || !errors_only {
        let mut headers = vec![
            "Source",
            "Access",
            "Index State",
            "Indexed",
            "Queued",
            "Processing",
            "Retry",
            "Failed",
            "Last Activity",
        ];
        if verbose {
            headers.push("Last Scan (completed)");
        }
        let mut table = Vec::new();
        for r in rows {
            let mut row = vec![
                s(&r["id"]),
                s(if r["access_state"].is_string() {
                    &r["access_state"]
                } else {
                    &r["status"]
                }),
                s(&r["index_state"]),
                i(&r["counts"]["indexed"]).to_string(),
                i(&r["queue"]["queued"]).to_string(),
                i(&r["queue"]["processing"]).to_string(),
                i(&r["queue"]["retry"]).to_string(),
                i(&r["counts"]["failed"]).to_string(),
                fmt_since(r["last_activity_at"].as_str()),
            ];
            if verbose {
                row.push(s(&r["last_scan_at"]));
            }
            table.push(row);
            if let Some(e) = r["last_error"].as_str() {
                if verbose || r["index_state"].as_str() != Some("completed") {
                    table.push(vec![String::new(), format!("last error: {e}")]);
                }
            }
            let detail = &r["last_error_detail"];
            if verbose && detail.is_object() {
                table.push(vec![
                    String::new(),
                    format!(
                        "last file error: {}: {}: {}",
                        s(&detail["path"]),
                        s(&detail["error_code"]),
                        s(&detail["error_message"])
                    ),
                ]);
            }
        }
        out += "== Sources ==\n";
        print!("{out}");
        out.clear();
        print_table(&headers, &table);
    }

    let detailed = verbose || errors_only;
    let limit = if detailed { 10 } else { 3 };
    let problems: Vec<&Value> = d["problems"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|p| detailed || p["severity"].as_str() != Some("info"))
        .collect();
    let errors: Vec<&Value> = d["recent_errors"]
        .as_array()
        .into_iter()
        .flatten()
        .take(limit)
        .collect();
    if !problems.is_empty() || !errors.is_empty() {
        let mut b = String::new();
        for p in problems {
            let mut scope = s(&p["scope"]);
            if let Some(id) = p["source_id"].as_str() {
                scope += &format!(" {id}");
            }
            b += &format!("[{}] {scope}: {}\n", s(&p["severity"]), s(&p["message"]));
        }
        if !errors.is_empty() {
            b += "Recent errors:\n";
            for e in errors {
                b += &format!(
                    "  {} {} {}: {}: {}\n",
                    s(&e["occurred_at"]),
                    s(&e["source_id"]),
                    s(&e["path"]),
                    s(&e["error_code"]),
                    s(&e["error_message"])
                );
            }
        }
        out += &panel("Problems", b.trim_end());
    } else if errors_only {
        out += "No problems found.\n";
    }
    out
}

pub fn status(a: &StatusArgs) -> Result<(), RagMonkError> {
    if a.watch && a.json {
        return Err(RagMonkError::usage(
            "--watch cannot be combined with --json",
        ));
    }
    if a.interval <= 0.0 || !a.interval.is_finite() {
        return Err(RagMonkError::usage("--interval must be > 0"));
    }
    let home = prepared_home()?;
    if !a.watch {
        let data = collect(&home)?;
        if a.json {
            return print_json(&data);
        }
        print!("{}", render(&data, a.errors, a.verbose, None));
        return Ok(());
    }
    let mut previous: Option<Sample> = None;
    loop {
        let data = collect(&home)?;
        let current = sample(&data);
        // Clear the screen and redraw.
        print!("\x1b[2J\x1b[H");
        print!(
            "{}",
            render(&data, a.errors, a.verbose, Some((previous, current)))
        );
        use std::io::Write;
        let _ = std::io::stdout().flush();
        previous = Some(current);
        std::thread::sleep(Duration::from_secs_f64(a.interval));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_match_reference() {
        assert_eq!(fmt_age(None), "-");
        assert_eq!(fmt_age(Some(5.4)), "5s ago");
        assert_eq!(fmt_age(Some(150.0)), "2m ago");
        assert_eq!(fmt_age(Some(5400.0)), "1.5h ago");
        assert_eq!(fmt_age(Some(172800.0)), "2.0d ago");
        assert_eq!(fmt_duration(Some(75.0)), "1m15s");
        assert_eq!(fmt_duration(Some(3725.0)), "1h02m");
        assert_eq!(signed(Some(3)), "+3");
        assert_eq!(signed(Some(-2)), "-2");
        assert_eq!(signed(None), "n/a");
    }
}
