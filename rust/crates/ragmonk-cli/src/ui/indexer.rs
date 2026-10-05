//! The UI's background indexer (`index_service.BackgroundIndexer`): at
//! most one index or rebuild run at a time on a worker thread, recording
//! progress events the `/events/indexing` stream tails.
//!
//! Each source pass takes the same `index` lock as `ragmonk index`, so a
//! UI run and a CLI run never index the same home concurrently.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use ragmonk_core::paths::Home;
use ragmonk_indexing::coordinator::{run_source, Options};
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::V2Layout;
use serde_json::{json, Map, Value};

use crate::load;
use crate::workflow::{control_plane, get_source};

const MAX_EVENTS: usize = 500;

#[derive(Default)]
struct State {
    active: bool,
    seq: u64,
    events: VecDeque<Value>,
    last_summary: Option<Value>,
}

#[derive(Clone, Default)]
pub struct Indexer {
    state: Arc<Mutex<State>>,
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

impl Indexer {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub fn is_running(&self) -> bool {
        self.lock().active
    }

    pub fn last_summary(&self) -> Value {
        self.lock().last_summary.clone().unwrap_or(Value::Null)
    }

    /// Events after `seq`, oldest first.
    pub fn events_since(&self, seq: u64) -> Vec<Value> {
        self.lock()
            .events
            .iter()
            .filter(|e| e["seq"].as_u64().unwrap_or(0) > seq)
            .cloned()
            .collect()
    }

    fn emit(&self, kind: &str, payload: Value) {
        let mut st = self.lock();
        st.seq += 1;
        let mut event = Map::new();
        event.insert("seq".into(), json!(st.seq));
        event.insert("kind".into(), json!(kind));
        event.insert("at".into(), json!(now()));
        if let Value::Object(p) = payload {
            event.extend(p);
        }
        if st.events.len() == MAX_EVENTS {
            st.events.pop_front();
        }
        st.events.push_back(Value::Object(event));
    }

    /// Starts a run; `false` when one is already running.
    pub fn start(&self, home: Home, source_id: Option<String>, rebuild: bool, fresh: bool) -> bool {
        {
            let mut st = self.lock();
            if st.active {
                return false;
            }
            st.active = true;
        }
        let this = self.clone();
        std::thread::Builder::new()
            .name("ragmonk-ui-indexer".into())
            .spawn(move || this.run(&home, source_id.as_deref(), rebuild, fresh))
            .map(|_| true)
            .unwrap_or_else(|_| {
                self.lock().active = false;
                false
            })
    }

    fn run(&self, home: &Home, source_id: Option<&str>, rebuild: bool, fresh: bool) {
        let mode = if rebuild { "rebuild" } else { "index" };
        self.emit(
            "run_started",
            json!({"mode": mode, "source_id": source_id, "fresh": fresh}),
        );
        let outcome = if rebuild {
            self.emit(
                "rebuild_started",
                json!({"source_id": source_id, "fresh": fresh}),
            );
            crate::ops_cmd::rebuild_sources(home, source_id, fresh, |o| {
                self.emit(
                    "scan_completed",
                    json!({
                        "source_id": o["id"],
                        "path": o["path"],
                        "scanned": o["scanned"],
                        "indexed": o["indexed"],
                        "failed": o["failed"],
                    }),
                );
            })
            .map(|outcomes| summary("rebuild", &outcomes))
        } else {
            self.index(home, source_id)
        };
        let summary = match outcome {
            Ok(s) => {
                self.emit("run_completed", s.clone());
                s
            }
            Err(e) => {
                self.emit("run_failed", json!({"error": e.message()}));
                json!({"mode": mode, "sources": [], "failed": 0, "error": e.message()})
            }
        };
        let mut st = self.lock();
        st.active = false;
        st.last_summary = Some(summary);
    }

    fn index(
        &self,
        home: &Home,
        source_id: Option<&str>,
    ) -> Result<Value, ragmonk_core::errors::RagMonkError> {
        let cfg = load(home)?;
        let mut cp = control_plane(home)?;
        let sources = match source_id {
            Some(id) => vec![get_source(&cp, id)?],
            None => cp.list_sources(true).map_err(|e| crate::ui::db_error(&e))?,
        };
        let layout = V2Layout::new(home);
        let registry =
            ragmonk_convert::registry_with(&cfg, &ragmonk_convert::RegistryOptions::for_home(home));
        let opts = Options::from_config(&cfg);
        let mut rows = Vec::new();
        ragmonk_indexing::progress::track(
            &home.index_progress(),
            "index",
            Some(sources.len() as i64),
            |tracker| -> Result<(), ragmonk_core::errors::RagMonkError> {
                for (i, source) in sources.iter().enumerate() {
                    tracker.begin_source(&source.id, Some(i as i64 + 1));
                    self.emit(
                        "scan_started",
                        json!({"source_id": source.id, "path": source.path}),
                    );
                    let lock = RunLock::acquire(
                        &home.locks_dir().join("index.lock"),
                        "index",
                        Some(&source.id),
                        opts.lock_timeout,
                    )?;
                    let r = run_source(&layout, &mut cp, source, &registry, &opts, tracker);
                    lock.release();
                    let r = r?;
                    let row = json!({
                        "source_id": source.id,
                        "path": source.path,
                        "offline": r.offline.is_some(),
                        "scanned": r.counts.scanned,
                        "indexed": r.indexed,
                        "failed": r.failed,
                    });
                    self.emit("scan_completed", row.clone());
                    rows.push(row);
                }
                Ok(())
            },
        )?;
        Ok(summary("index", &rows))
    }
}

fn summary(mode: &str, rows: &[Value]) -> Value {
    let failed: i64 = rows.iter().map(|r| r["failed"].as_i64().unwrap_or(0)).sum();
    let sources: Vec<Value> = rows
        .iter()
        .map(|r| {
            let mut r = r.clone();
            if let Some(o) = r.as_object_mut() {
                if let Some(id) = o.remove("id") {
                    o.insert("source_id".into(), id);
                }
                o.remove("linked");
            }
            r
        })
        .collect();
    json!({"mode": mode, "sources": sources, "failed": failed})
}
