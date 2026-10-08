# ADR 0019: Indexing progress and status observability

Status: accepted

## Scope

Indexing progress and status observability. The daemon lifecycle is
ADR 0020; the filesystem watcher (`notify` with a polling fallback) is
ADR 0021.

## Progress snapshot (`ragmonk_indexing::progress`)

- **Format.** `<home>/index_progress.json`: schema version 1, 19 keys,
  ISO-8601 UTC timestamps.
- **Writes.** Writes are atomic: a per-process/thread temp file, then
  rename. Counter updates are coalesced to at most one write per second.
  Source and stage changes and the final snapshot are always written.
- **Reading.** The reader is tolerant: a missing, malformed or
  newer-schema file reads as `None`, and wrongly typed fields fall back to
  their defaults.
- **The tracker.**
  - `ProgressTracker` implements the coordinator's `Progress` sink.
  - `track()` writes the final snapshot on both success (`completed`) and
    error (`failed: …`, truncated to 500 characters).
  - `ProgressEvent::FileDone` carries the file's outcome (indexed,
    failed, retry or skipped).
- **Heartbeats.** A process-wide `heartbeat()` keeps a working run from
  reading as stalled. The embedding and linking loops call it, so a long
  finalizer stage doesn't look like a hang.

## Status (`ragmonk_indexing::status`)

Pure verdict functions build the status JSON:

- **`indexer_state_from`.** It combines the lock view, the snapshot, a
  process-liveness probe, the threshold and the clock into one state:
  - `running` when the lock is held, or a live process's snapshot shows a
    run in flight (the lock is released briefly between sources);
  - `stalled` when the heartbeat is older than
    `indexing.status_stall_threshold_seconds`;
  - `crashed` when the snapshot says running, the lock is free and the
    process is gone;
  - `idle` otherwise.

  It never disturbs the lock.
- **`derive_index_state`.** Per-source precedence: offline > stalled >
  indexing > retrying > errors > waiting > completed > idle.
- **Problems, health and queues.** `derive_problems`, `derive_health` and
  `merge_queue_stats` build the problem list, the health verdict and the
  queue-statistics merge.

I/O wrappers:

- **`indexer_state`** reads the real lock file and snapshot.
  `is_process_alive` uses `sysinfo`, a safe cross-platform API: the
  workspace forbids `unsafe`, and `kill(pid, 0)` would need it.
- **`collect_status`** builds the status payload from the stores:
  health, indexer, merged queue, recent errors, problems, per-source rows
  and totals. The CLI adds the tokenizer and server-backend sections it
  renders.

**Retry state.** Retry state lives on file rows (`status = 'retry'`,
`attempt_count`, `next_attempt_at`), not in a job queue. The per-source
"queue" is derived from those rows:

- retry, failed and completed counts;
- the earliest next retry and the maximum attempt count;
- the latest error of a file that is waiting to retry.

`queued` and `processing` only exist while a run is in flight and are
reported by the progress snapshot. Every failed or retrying file is
recorded in `index_errors`, which feeds recent errors.

## Dependency

`sysinfo` 0.37 (MIT), default features off, `system` only. It meets the
dependency policy (ADR 0003): maintained, permissive license, and no
system packages on Windows, Linux or macOS. It replaces an `unsafe`
syscall wrapper.

## Evidence

- **`fixtures/expected/status.json`** runs the verdict functions over
  fixed scenarios. Lock, snapshot and liveness inputs are substituted;
  nothing is mocked inside the verdicts:
  - 12 indexer scenarios: free, held, fresh, stale, between sources,
    crashed, held by another pid, owner without pid, completed, failed,
    unknown lock, naive timestamp;
  - 12 per-source index-state cases;
  - 6 problem/health cases;
  - 2 queue merges.

  The output must equal the expected JSON exactly.
- **Integration tests:**
  - A tracked run over a source with an indexed, a retrying and a
    permanently failing file writes the right final snapshot. The status
    report then shows the counts, `retrying`, the latest retry error, both
    problems, `degraded` health and two recent errors.
  - With a real lock and snapshot, the indexer reads as running (live
    pid), stalled (held lock, 600 s stale heartbeat) and crashed (dead pid,
    free lock).
- **Unit tests:** snapshot round trip and tolerance, write coalescing, and
  failure recording plus heartbeat in `track`.
