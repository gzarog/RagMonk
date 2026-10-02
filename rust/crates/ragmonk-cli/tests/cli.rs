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
