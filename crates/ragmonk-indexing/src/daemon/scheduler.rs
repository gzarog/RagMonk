//! The daemon's trigger coalescing, without threads
//! (`Daemon.enqueue_source`, `_build_scan_request` and
//! `_settle_pending_state`). Each source is in one of four states: no
//! entry, `queued`, `running`, or `running_followup`. A burst of triggers
//! becomes at most one queued pass plus one follow-up. Touched paths,
//! force-full and the first reason accumulate until the next pass takes
//! them.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::path::PathBuf;

use super::{ScanRequest, FORCE_FULL_REASONS, MAX_TARGETED_PATHS};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Pending {
    Queued,
    Running,
    RunningFollowup,
}

impl Pending {
    fn as_str(self) -> &'static str {
        match self {
            Pending::Queued => "queued",
            Pending::Running => "running",
            Pending::RunningFollowup => "running_followup",
        }
    }
}

#[derive(Debug, Default)]
pub struct Scheduler {
    queue: VecDeque<String>,
    pending: HashMap<String, Pending>,
    touched: HashMap<String, BTreeSet<PathBuf>>,
    force_full: HashMap<String, bool>,
    reasons: HashMap<String, String>,
}

impl Scheduler {
    /// Records a trigger. True when it put a new entry on the queue.
    pub fn enqueue(&mut self, source_id: &str, reason: &str, paths: &[PathBuf]) -> bool {
        if !paths.is_empty() {
            self.touched
                .entry(source_id.into())
                .or_default()
                .extend(paths.iter().cloned());
        }
        if FORCE_FULL_REASONS.contains(&reason) {
            self.force_full.insert(source_id.into(), true);
        }
        self.reasons
            .entry(source_id.into())
            .or_insert_with(|| reason.into());
        match self.pending.get(source_id).copied() {
            None => {
                self.pending.insert(source_id.into(), Pending::Queued);
                self.queue.push_back(source_id.into());
                true
            }
            Some(Pending::Running) => {
                self.pending
                    .insert(source_id.into(), Pending::RunningFollowup);
                false
            }
            Some(Pending::Queued | Pending::RunningFollowup) => false,
        }
    }

    /// Pops the next source and marks it running.
    pub fn start_next(&mut self) -> Option<String> {
        let id = self.queue.pop_front()?;
        self.pending.insert(id.clone(), Pending::Running);
        Some(id)
    }

    /// Takes what accumulated for `source_id`: the next pass's request.
    pub fn take_request(&mut self, source_id: &str) -> ScanRequest {
        let touched = self.touched.remove(source_id).unwrap_or_default();
        let force_full = self.force_full.remove(source_id).unwrap_or(false);
        let reason = self
            .reasons
            .remove(source_id)
            .unwrap_or_else(|| "daemon".into());
        let full = force_full || touched.is_empty() || touched.len() > MAX_TARGETED_PATHS;
        ScanRequest {
            source_id: source_id.into(),
            reason,
            full,
            changed_paths: if full { BTreeSet::new() } else { touched },
        }
    }

    /// A pass finished. A follow-up, if one arrived, goes to the back of
    /// the queue (true).
    pub fn settle(&mut self, source_id: &str) -> bool {
        if self.pending.remove(source_id) == Some(Pending::RunningFollowup) {
            self.pending.insert(source_id.into(), Pending::Queued);
            self.queue.push_back(source_id.into());
            return true;
        }
        false
    }

    pub fn has_queued(&self) -> bool {
        !self.queue.is_empty()
    }

    pub fn queue(&self) -> Vec<String> {
        self.queue.iter().cloned().collect()
    }

    /// The reference's state name, if any.
    pub fn state_of(&self, source_id: &str) -> Option<&'static str> {
        self.pending.get(source_id).map(|p| p.as_str())
    }

    pub fn is_idle(&self) -> bool {
        self.queue.is_empty() && self.pending.is_empty()
    }

    /// Drops everything queued (shutdown).
    pub fn clear_queue(&mut self) {
        self.queue.clear();
    }
}
