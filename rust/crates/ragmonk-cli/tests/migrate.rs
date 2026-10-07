//! `migrate-to-rust-v2 --check/--execute` (RUST-15).
//!
//! Local mode end to end, and against a real OpenSearch/Elasticsearch
//! cluster when `RAGMONK_TEST_OPENSEARCH_URL` /
//! `RAGMONK_TEST_ELASTICSEARCH_URL` are set (required in CI with
//! `RAGMONK_REQUIRE_LIVE_SERVER=1`). WARNING: the live test deletes the
//! legacy index names of the unique prefix it creates; never point it at
//! a real deployment.

use std::path::Path;
use std::process::{Command, Output, Stdio};

use serde_json::Value;

fn ragmonk(home: &Path, args: &[&str]) -> Output {
    let mut c = Command::new(env!("CARGO_BIN_EXE_ragmonk"));
    for (k, _) in std::env::vars() {
        if k.starts_with("RAGMONK_") && !k.starts_with("RAGMONK_TEST_") {
            c.env_remove(k);
        }
    }
    c.args(args)
        .env("RAGMONK_HOME", home)
        .env("RAGMONK_SEARCH__SEMANTIC", "false")
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

fn ok(o: &Output) -> String {
    assert!(o.status.success(), "{o:?}");
    String::from_utf8_lossy(&o.stdout).into_owned()
}

fn data(o: &Output) -> Value {
    serde_json::from_str::<Value>(&ok(o)).unwrap()["data"].clone()
}

fn indexed_home(dir: &Path) -> std::path::PathBuf {
    let home = dir.join("home");
    let src = dir.join("src");
    std::fs::create_dir_all(&src).unwrap();
    std::fs::write(src.join("a.py"), "def alpha():\n    return 1\n").unwrap();
    ok(&ragmonk(&home, &["init"]));
    ok(&ragmonk(&home, &["source", "add", src.to_str().unwrap()]));
    ok(&ragmonk(&home, &["index"]));
    home
}

#[test]
fn execute_resets_local_v2_state_after_confirmation() {
    let dir = tempfile::tempdir().unwrap();
    let home = indexed_home(dir.path());
    let projects = home.join("v2").join("projects");
    assert_eq!(std::fs::read_dir(&projects).unwrap().count(), 1);

    let check = data(&ragmonk(
        &home,
        &["migrate-to-rust-v2", "--check", "--json"],
    ));
    let plan = &check["execute"];
    assert_eq!(plan["local_reset"].as_array().unwrap().len(), 1);
    assert!(plan["server"].is_null(), "local mode has no server step");
    assert_eq!(
        plan["sources_marked_for_full_rebuild"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let fp = plan["fingerprint"].as_str().unwrap().to_owned();
    let text = ok(&ragmonk(&home, &["migrate-to-rust-v2", "--check"]));
    assert!(text.contains(&format!("Plan fingerprint: {fp}")), "{text}");
    assert!(text.contains("No legacy-index rollback is kept"), "{text}");

    // Without a terminal or a fingerprint nothing happens.
    let o = ragmonk(&home, &["migrate-to-rust-v2", "--execute"]);
    assert_eq!(o.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&o.stderr).contains("--confirm <fingerprint>"));
    let o = ragmonk(
        &home,
        &["migrate-to-rust-v2", "--execute", "--confirm", "deadbeef"],
    );
    assert_eq!(o.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&o.stderr).contains("plan changed"));
    assert_eq!(
        std::fs::read_dir(&projects).unwrap().count(),
        1,
        "untouched"
    );
    let o = ragmonk(&home, &["migrate-to-rust-v2", "--confirm", &fp]);
    assert_eq!(o.status.code(), Some(2), "--confirm needs --execute");

    let r = data(&ragmonk(
        &home,
        &[
            "migrate-to-rust-v2",
            "--execute",
            "--confirm",
            &fp,
            "--json",
        ],
    ));
    assert_eq!(r["local_reset"].as_array().unwrap().len(), 1);
    assert_eq!(r["needs_full_rebuild"].as_array().unwrap().len(), 1);
    assert!(r["server"].is_null());
    assert_eq!(std::fs::read_dir(&projects).unwrap().count(), 0);

    // The registration survives, the index is gone until rebuilt.
    let st = data(&ragmonk(&home, &["status", "--json"]));
    assert_eq!(st["sources"].as_array().unwrap().len(), 1);
    let s = data(&ragmonk(&home, &["symbol", "alpha", "--json"]));
    assert!(s["matches"].as_array().unwrap().is_empty());
    ok(&ragmonk(&home, &["index"]));
    let s = data(&ragmonk(&home, &["symbol", "alpha", "--json"]));
    assert_eq!(s["matches"][0]["qualified_name"], "a.alpha");

    // A repeated check reflects the new state, so an old fingerprint no
    // longer matches.
    let again = data(&ragmonk(
        &home,
        &["migrate-to-rust-v2", "--check", "--json"],
    ));
    assert_eq!(again["execute"]["local_reset"].as_array().unwrap().len(), 1);
}

fn live_url(var: &str) -> Option<String> {
    match std::env::var(var) {
        Ok(u) if !u.is_empty() => Some(u),
        _ => {
            assert!(
                std::env::var("RAGMONK_REQUIRE_LIVE_SERVER").as_deref() != Ok("1"),
                "{var} is required when RAGMONK_REQUIRE_LIVE_SERVER=1"
            );
            eprintln!("skipping: {var} not set");
            None
        }
    }
}

fn http(url: &str, method: &str, body: Option<&str>) -> u16 {
    let client = reqwest::blocking::Client::new();
    let req = match method {
        "PUT" => client.put(url),
        "DELETE" => client.delete(url),
        "HEAD" => client.head(url),
        _ => client.get(url),
    };
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(b.to_owned()),
        None => req,
    };
    req.send().unwrap().status().as_u16()
}

fn live_migration(engine: &str, base: &str) {
    let dir = tempfile::tempdir().unwrap();
    let home = indexed_home(dir.path());
    let prefix = format!(
        "rmtest{}{}",
        std::process::id(),
        engine.chars().next().unwrap()
    );
    for suffix in ["files", "content", "relationships"] {
        let name = format!("{base}/{prefix}-{suffix}");
        assert!(http(&name, "PUT", Some("{}")) < 300);
        assert!(
            http(
                &format!("{name}/_doc/1?refresh=true"),
                "PUT",
                Some(r#"{"x":1}"#)
            ) < 300
        );
    }
    let set = |k: &str, v: &str| ok(&ragmonk(&home, &["config", "set", k, v]));
    set("storage.mode", "server");
    set("storage.server.engine", engine);
    set("storage.server.url", base);
    set("storage.server.index_prefix", &prefix);

    let check = data(&ragmonk(
        &home,
        &["migrate-to-rust-v2", "--check", "--json"],
    ));
    let server = &check["execute"]["server"];
    let legacy: Vec<&str> = server["legacy_indexes"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|i| i["name"].as_str())
        .filter(|n| n.starts_with(&prefix))
        .collect();
    assert_eq!(legacy.len(), 3, "{server}");
    let fp = check["execute"]["fingerprint"].as_str().unwrap().to_owned();

    let r = data(&ragmonk(
        &home,
        &[
            "migrate-to-rust-v2",
            "--execute",
            "--confirm",
            &fp,
            "--json",
        ],
    ));
    let deleted = r["server"]["legacy_deleted"].as_array().unwrap();
    assert!(
        deleted.iter().any(|d| d == &format!("{prefix}-content")),
        "{r}"
    );
    for suffix in ["files", "content", "relationships"] {
        assert_eq!(
            http(&format!("{base}/{prefix}-{suffix}"), "HEAD", None),
            404
        );
    }
    let v2 = r["server"]["v2_created"].as_array().unwrap().len()
        + r["server"]["v2_verified"].as_array().unwrap().len();
    assert!(v2 > 0, "{r}");
    for name in server["v2_indexes"].as_array().unwrap() {
        let name = name.as_str().unwrap();
        assert_eq!(http(&format!("{base}/{name}"), "HEAD", None), 200, "{name}");
        http(&format!("{base}/{name}"), "DELETE", None);
    }
}

#[test]
fn live_execute_deletes_legacy_indexes_and_initializes_v2() {
    if let Some(u) = live_url("RAGMONK_TEST_OPENSEARCH_URL") {
        live_migration("opensearch", u.trim_end_matches('/'));
    }
    if let Some(u) = live_url("RAGMONK_TEST_ELASTICSEARCH_URL") {
        live_migration("elasticsearch", u.trim_end_matches('/'));
    }
}
