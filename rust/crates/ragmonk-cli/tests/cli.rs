use std::process::Command;

fn ragmonk() -> Command {
    Command::new(env!("CARGO_BIN_EXE_ragmonk"))
}

#[test]
fn version_json_envelope() {
    let out = ragmonk().args(["version", "--json"]).output().unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["schema_version"], "1");
    assert!(v["data"]["version"].is_string());
}

#[test]
fn unknown_command_exits_with_invalid_arguments_code() {
    // Python's Typer CLI returns 2 (EXIT_INVALID_ARGUMENTS); see the
    // committed baseline step cli-basics/unknown-command.
    let out = ragmonk().arg("definitely-not-a-command").output().unwrap();
    assert_eq!(out.status.code(), Some(2));
}

fn with_home(home: &std::path::Path) -> Command {
    let mut cmd = ragmonk();
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("RAGMONK_") {
            cmd.env_remove(key);
        }
    }
    cmd.env("RAGMONK_HOME", home);
    cmd
}

#[test]
fn config_show_get_set_round_trip() {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().join("home");
    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["config", "show"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(text.starts_with("version: 1\nruntime:\n  log_level: info\n"));
    assert!(
        home.join("logs").is_dir(),
        "config show ensures the runtime layout"
    );

    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["config", "set", "runtime.max_workers", "3"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["config", "get", "runtime.max_workers"])
        .output()
        .unwrap();
    assert_eq!(String::from_utf8(out.stdout).unwrap(), "3\n");
    let written = std::fs::read_to_string(home.join("config.yaml")).unwrap();
    assert!(written.contains("  max_workers: 3\n"));
}

#[test]
fn config_errors_use_python_exit_codes() {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().join("home");
    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["config", "get", "no.such.key"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8(out.stderr).unwrap(),
        "Error: unknown config key: no.such.key\n"
    );

    std::fs::create_dir_all(&home).unwrap();
    std::fs::write(
        home.join("config.yaml"),
        "storage:\n  server:\n    url: https://user:hunter2@es:9200\n",
    )
    .unwrap();
    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["config", "show"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(3));
    let stderr = String::from_utf8(out.stderr).unwrap();
    assert!(stderr.starts_with("Error: invalid configuration: storage.server.url: Value error"));
    assert!(!stderr.contains("hunter2"), "credential leaked: {stderr}");
}

fn copy_tree(from: &std::path::Path, to: &std::path::Path) {
    std::fs::create_dir_all(to).unwrap();
    for e in std::fs::read_dir(from).unwrap() {
        let e = e.unwrap();
        let dest = to.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy_tree(&e.path(), &dest);
        } else {
            std::fs::copy(e.path(), dest).unwrap();
        }
    }
}

#[test]
fn migrate_check_then_import_on_python_home() {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().join("home");
    copy_tree(
        &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../compat/fixtures/v1_home"),
        &home,
    );
    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["migrate-to-rust-v2", "--check", "--json"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["data"]["sources"].as_array().unwrap().len(), 2);
    assert!(!home.join("v2").exists(), "--check is read-only");

    let out = with_home(&home)
        .current_dir(tmp.path())
        .args(["migrate-to-rust-v2", "--import-sources", "--json"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["data"]["imported"].as_array().unwrap().len(), 2);
    assert_eq!(v["data"]["needs_full_rebuild"].as_array().unwrap().len(), 2);
    assert!(home.join("v2").join("control.db").is_file());

    // Exactly one action is required.
    let out = with_home(&home)
        .args(["migrate-to-rust-v2"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
}
