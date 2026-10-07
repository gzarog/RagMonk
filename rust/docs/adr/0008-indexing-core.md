# ADR 0008: Indexing core (RUST-04)

Status: accepted

- Scan/ignore/classify/fingerprint semantics match the Python reference (`sources/{scanner,ignore,detector,fingerprint}.py`); verified by golden fixtures generated from Python (`compat/golden/indexing.json`).
- Diff states: new, changed, unchanged, unchanged-restat (stat drift, same hash), moved (same hash, new path; counted as moved + unchanged but re-extracted), unseen (incomplete scan: kept, never deleted), deleted (complete scan only).
- Incremental builds carry the active build forward with one set-based `carry_forward_except`; only changed/moved/deleted rows are excluded. Incomplete builds are never published.
- Retry: exponential backoff 2s·2^(n-1), capped at 300 s, 5 attempts; stored in knowledge schema v2 (`attempt_count`, `next_attempt_at`), migrated with backup.
- `RunLock` uses an OS file lock (`File::try_lock`) with owner metadata compatible with the Python lock file layout.
- Symlinks/junctions are not followed by default.
- Until RUST-05/07 the registry has only a raw processor (`raw-1`). Cross-source links are not carried forward; the linker recomputes them in RUST-08.
- Concurrency is bounded: `sync_channel` workers plus a single SQLite writer.
