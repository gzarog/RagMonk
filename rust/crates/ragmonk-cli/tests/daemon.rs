//! `ragmonk daemon` process lifecycle: start, status, a second start, stop.

use std::path::Path;
use std::process::{Command, Output};

/// Output goes to files, not pipes: on Windows the detached daemon
/// inherits the CLI's inheritable handles, so a pipe would never reach EOF
/// while the daemon runs.
fn ragmonk(home: &Path, args: &[&str]) -> Output {
    let dir = tempfile::tempdir().unwrap();
    let (out, err) = (dir.path().join("out"), dir.path().join("err"));
    let status = Command::new(env!("CARGO_BIN_EXE_ragmonk"))
        .args(args)
        .env("RAGMONK_HOME", home)
        .stdin(std::process::Stdio::null())
        .stdout(std::fs::File::create(&out).unwrap())
        .stderr(std::fs::File::create(&err).unwrap())
        .status()
        .unwrap();
    Output {
        status,
        stdout: std::fs::read(&out).unwrap(),
        stderr: std::fs::read(&err).unwrap(),
    }
}

fn status(home: &Path) -> serde_json::Value {
    let out = ragmonk(home, &["daemon", "status", "--json"]);
    assert!(out.status.success(), "{out:?}");
    serde_json::from_slice::<serde_json::Value>(&out.stdout).unwrap()["data"].clone()
}

#[test]
fn start_status_stop() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");

    let s = status(&home);
    assert_eq!(s["running"], false);
    assert_eq!(s["pid"], serde_json::Value::Null);
    let keys: Vec<_> = s.as_object().unwrap().keys().cloned().collect();
    // Same key order as the reference payload.
    assert_eq!(
        keys,
        [
            "running",
            "pid",
            "started_at",
            "uptime_seconds",
            "last_reconciliation_at",
            "sources"
        ]
    );

    let out = ragmonk(&home, &["daemon", "start"]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(out.status.success(), "{out:?}");
    assert!(text.starts_with("Daemon started (pid "), "{text}");

    let s = status(&home);
    assert_eq!(s["running"], true);
    let pid = s["pid"].as_i64().unwrap();
    assert!(s["uptime_seconds"].as_f64().unwrap() >= 0.0);
    assert!(home.join("daemon_health.json").is_file());

    let out = ragmonk(&home, &["daemon", "start"]);
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        format!("Daemon already running (pid {pid})")
    );
    // A foreground run refuses while another daemon owns the home.
    let out = ragmonk(&home, &["daemon", "run"]);
    assert!(!out.status.success());

    let out = ragmonk(&home, &["daemon", "stop"]);
    assert!(out.status.success(), "{out:?}");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        format!("Daemon stopped (pid {pid})")
    );
    assert!(!home.join("daemon.pid").exists());
    assert_eq!(status(&home)["running"], false);
    let out = ragmonk(&home, &["daemon", "stop"]);
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        "Daemon is not running"
    );
}
