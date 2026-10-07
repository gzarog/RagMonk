# ADR 0021: Filesystem watcher, network polling and targeted passes (RUST-11, slice 3)

Status: accepted

## Context

Slice 2 ported the daemon, but every pass re-scanned the whole source. The
reference (`watcher/local.py`, `watcher/network.py`,
`watcher/debounce.py`) reacts to changes as they happen. It passes the
exact touched paths to a targeted pass (`IndexCoordinator._run_targeted`).

## Decisions

### Targeted passes (`coordinator::run_source_with`)

- **Scanning.** `scan::scan_targets` examines only the named paths:
  - There is no directory walk, so nothing can be silently truncated.
  - A path is skipped when it is a symlink (unless symlinks are
    followed), lies outside the root, is a directory, or is ignored.
  - A path that no longer exists is reported as missing.
- **Diffing.** The baseline is cut down to the rows for the named paths,
  then the normal diff runs. As a result:
  - A row is deleted only when its path was named and is gone. Absence is
    never treated as deletion.
  - A rename is kept as a move (same file id) only when both halves are
    in the same batch. Naming only the new half indexes it as new; the old
    row stays until its own path is named or a full pass runs. This is the
    reference's trade-off.
  - Rows for every other file are left untouched. An incremental pass
    already updates the active build per decision, so this needs no new
    write path.
- **Counting.** `scanned` counts present plus missing paths, as the
  reference does.
- **Full rebuilds.** Targets are ignored when the plan is a full rebuild,
  for example after a version change. A new build must see the whole tree.
- **Daemon requests.** `SourceResult::targeted` marks targeted passes.
  `CoordinatorRunner` runs a targeted pass whenever the scheduler's
  request carries touched paths and is not forced full.

### Watchers (`ragmonk_indexing::watcher`)

- **Local sources (`local::LocalWatcher`).**
  - Uses `notify` 8.2 (inotify, FSEvents, ReadDirectoryChangesW),
    recursive.
  - Reports every create, modify, remove and rename (both halves),
    debounced per path. Paths that are directories are dropped.
  - Native watching has two fallbacks: if the native watcher cannot be
    created, or cannot watch this root (for example the inotify watch
    limit is reached), `notify`'s `PollWatcher` takes over (2 s
    interval). The reference has no fallback.
  - A missing root fails to attach. The next reconciliation retries it, as
    in the reference.
- **Debounce (`debounce::Debouncer`).**
  - Each notify restarts that key's quiet period (`indexing.debounce_ms`),
    and keys never delay each other.
  - One timer thread serves every key, instead of one timer thread per key.
  - `flush` drops pending keys on shutdown.
- **Network sources (`network::NetworkWatcher`).**
  - Fingerprints the tree (path to size and mtime) through the same
    scanner and ignore rules, every `indexing.network_poll_seconds`.
  - Each tick that finds a change reports exactly the changed paths, once.
  - An unreachable root fingerprints as empty, and the pass decides what
    offline means.
- **Daemon wiring.**
  - Watchers are attached at startup for enabled sources.
  - Reconciliation re-attaches any watcher that failed.
  - Shutdown stops the watchers first, flushing pending debounced paths.
    The next startup pass covers anything dropped.
  - Callbacks hold a `Weak` reference to the daemon, so watchers never
    keep it alive.
- **`indexing.watch`.** The reference never reads this setting. Rust
  honors it: when it is false, no watchers are attached and only
  reconciliation runs.

## Parity and tests

- **Golden fixtures.** `compat/tools/gen_watcher_golden.py` writes
  `compat/golden/watcher.json`:
  - It indexes a tree with the reference, then runs 6 targeted steps
    through `run_source_pass(scan_request=ScanRequest(full=False))`.
  - The steps cover: edit plus new file plus ignored file plus a
    directory; a same-batch rename; a delete plus an unknown path; a split
    rename (new half only); unchanged plus outside the root; and the old
    half named later.
  - Each step records the pass counters and the indexed files.
  - The fixtures also hold 5 `network.diff` cases.
- **Parity results.** Rust matches all of them, counters and files
  included (`tests/targeted_golden.rs`).
- **Unit and end-to-end tests.**
  - Debounce: collapsing, independent keys, flush, zero quiet period.
  - Native and polling-fallback local watching.
  - Network polling with exact changed paths, and the unreachable root.
  - Daemon end-to-end: a file written under a watched local root, and
    under a polled network root, produces one targeted pass that indexes
    only that file.
