# ADR 0020: Daemon lifecycle, PID ownership and reconciliation (RUST-11, slice 2)

Status: accepted

## Context

The reference daemon (`service/daemon.py`, `service/pid.py`,
`service/health.py`, `cli/daemon.py`) runs indexing passes on one worker
thread. A second thread re-checks every source on a timer. A separate CLI
process finds the daemon through `daemon.pid` and reports on it from
`daemon_health.json`. This slice ports all of that except the watchers,
which are slice 3.

## Decisions

### Worker and scheduling (`ragmonk_indexing::daemon`)

- **Threads.** One worker thread runs passes from a FIFO queue. A
  reconciliation thread enqueues every enabled source every
  `indexing.reconciliation_interval_seconds` (default 900). One mutex and
  one condvar replace the reference's queue, `Event` and locks. Every
  wait wakes at once on stop.
- **Coalescing (`daemon::scheduler::Scheduler`).** This is the reference's
  state machine, with no threads, so it can be replayed against Python:
  - Each source has no entry, or is `queued`, `running` or
    `running_followup`.
  - A burst of triggers becomes at most one queued pass plus one
    follow-up.
  - Touched paths, force-full, and the first trigger reason of a batch
    accumulate. The pass takes them when it starts.
  - `startup` and `reconciliation` force a full pass. So do more than 200
    touched paths, or none.
- **Fairness across sources.** A follow-up joins the back of the queue,
  so a source that keeps changing never starves the others. The tests
  check this order.
- **Lock contention.** Every pass takes `locks/index.lock` with operation
  `daemon`, waiting up to `indexing.lock_timeout_seconds`. On timeout the
  worker backs off for 5 s (cut short by stop), then re-enqueues the
  source with reason `lock_contention`. The source is still `running` at
  that point, so the re-enqueue becomes its follow-up. The scan request is
  built only after the lock is won, so touched paths survive the
  contention.
- **Database connections.** Two separate connections are used:
  - The worker owns its connection through a `PassRunner`. The production
    runner is `CoordinatorRunner`: its own `ControlPlane`, the full
    registry, and `run_source`.
  - The daemon's own reads (which sources exist, the health snapshot) go
    through a separate `Catalog` connection.
  - Neither connection is shared across threads.
- **Progress.** Each pass runs under `progress::track(operation="daemon",
  source_total=1)`, so `index_progress.json` and the status verdicts from
  slice 1 cover daemon passes.
- **Graceful stop.**
  - New triggers are refused, and queued passes are dropped.
  - A pass already running finishes. It commits per file anyway.
  - Both threads are joined, and a final health snapshot is written.
  - The reference's worker drains its queue after stop. That behavior is
    incidental: its docstring describes the in-flight-only behavior that
    Rust implements.

### Files

- **`daemon.pid`.** The file is `{"pid", "started_at"}`. The reader is
  tolerant, like the reference's `int()`/`str()` coercions: a string pid
  is accepted, a float is truncated, and a non-string `started_at` is
  stringified. Anything else reads as "no daemon".
- **Stale PID files.** A dead PID counts as "not running". Liveness comes
  from `sysinfo`: no `unsafe` code, and a zombie counts as dead.
- **`daemon_health.json`.** The fields are the same as the reference's.
  Writes are atomic, with a per-process and per-thread temp name. A
  source entry with unknown keys makes the snapshot unreadable, as
  `SourceWatchStatus(**s)` does.

### CLI (`ragmonk daemon …`)

- **`start`.**
  - Spawns `ragmonk daemon run` detached: its own process group on POSIX,
    `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` on Windows.
  - Output goes to `logs/daemon.out.log`.
  - Records the child's PID, then waits up to 30 s for its first health
    snapshot.
  - On failure it removes the PID file and names the log.
  - Unlike the reference, it deletes any old health file first, so a
    snapshot left by a previous run cannot pass as "healthy".
- **`stop`.** On POSIX it sends SIGTERM, through `sysinfo`. On Windows it
  hard-terminates the process, as the reference does. It waits up to 15 s
  and removes the PID file. Messages and exit codes match the reference.
- **`run`.**
  - Runs in the foreground.
  - Refuses to start if another live daemon owns the home.
  - When started by hand, records its own PID, and removes the file on
    exit only if the file still names this process.
  - SIGTERM and SIGINT are caught through `signal-hook` on Unix, which
    triggers a graceful stop.
- **`status [--json]`.**
  - The JSON payload is the reference's, with the same keys in the same
    order.
  - The text output is the reference's without Rich markup.
  - The web-dashboard hint is left out, because the UI is not ported.
- **`restart`.** Runs `stop`, then `start`.
- **Windows caveat.** The spawned daemon inherits the CLI's inheritable
  handles. If the caller pipes `daemon start`'s output, the pipe stays
  open while the daemon runs. Python's `close_fds=True` avoids this; Rust's
  std has no stable equivalent. The process test redirects output to
  files for this reason.
- **Not ported.** The `--ui` option waits for the UI port.

## Parity and tests

- **Golden fixtures.** `compat/tools/gen_daemon_golden.py` writes
  `compat/golden/daemon.json`, covering:
  - uptime text, 14 cases;
  - `read_pid_file`, 10 raw files;
  - `read_health`, 7 raw files;
  - 5 trigger scripts replayed on the reference `Daemon`'s own
    coalescing methods, recording the queue, the per-source states and
    each pass's request after every step.
- **Parity results.** Rust matches all of them, in
  `tests/daemon_golden.rs`.
- **Behavior tests (`tests/daemon.rs`).** These use a scripted runner and
  cover:
  - bursts coalescing;
  - fairness;
  - forced full passes;
  - lock contention with backoff, retry and touched paths kept;
  - the reconciliation timer;
  - the health snapshot, including offline sources;
  - graceful stop;
  - a real `CoordinatorRunner` pass, publishing progress.
- **Process test (`ragmonk-cli/tests/daemon.rs`).** Runs `start`, then
  `status --json`, a second `start`, `run` while another daemon is
  running, `stop`, and `stop` again.

## Deferred

- **Slice 3.** Targeted passes, the local watcher, network polling and
  debounce: done in slice 3 ([ADR 0021](0021-watcher-and-targeted-passes.md)).
- **Server mode.** The daemon's startup health check for server mode
  waits until the server backends are wired into the indexing path.
