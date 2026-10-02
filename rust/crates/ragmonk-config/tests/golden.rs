//! Replays `rust/compat/golden/core_config.json`, generated from the
//! Python reference by `rust/compat/tools/gen_core_golden.py`.

use ragmonk_config::loader::{dump_yaml, load_config, LoadOptions};
use serde_json::Value;

/// Documented divergences (ADR 0005). Each must still fail/succeed in the
/// stated way so a silent behavior change is caught.
fn known_divergence(name: &str) -> Option<&'static str> {
    match name {
        // PyYAML's syntax-error text is not reproduced; prefix and exit code are.
        "yaml_syntax_error" => Some("prefix"),
        // PyYAML appends a source-position snippet to constructor errors;
        // the first line (what failed and why) must match exactly.
        "yaml_value_eq" => Some("first_line"),
        // Python ints are unbounded; config integers are 64-bit here.
        "bigint" => Some("rust_rejects"),
        // Python int() accepts non-ASCII decimal digits; only ASCII here.
        "env_unicode_digits" => Some("rust_rejects"),
        _ => None,
    }
}

#[test]
fn config_loading_matches_python() {
    let path = format!(
        "{}/../../compat/golden/core_config.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let cases: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    assert!(cases.len() > 200, "corpus unexpectedly small");
    let mut failures = Vec::new();
    for case in &cases {
        let name = case["name"].as_str().unwrap();
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        let cwd = tmp.path().join("cwd");
        std::fs::create_dir_all(&home).unwrap();
        std::fs::create_dir_all(&cwd).unwrap();
        if let Some(user) = case["user"].as_str() {
            std::fs::write(home.join("config.yaml"), user).unwrap();
        }
        if let Some(project) = case["project"].as_str() {
            std::fs::write(cwd.join(".ragmonk.yaml"), project).unwrap();
        }
        let environ: Vec<(String, String)> = case["env"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
            .collect();
        let opts = LoadOptions {
            home: Some(home.clone()),
            cwd: Some(cwd.clone()),
            environ: Some(environ),
            cli_overrides: None,
        };
        let actual = match load_config(&opts) {
            Ok(cfg) => Ok(dump_yaml(&cfg)),
            Err(e) => Err((
                e.message()
                    .replace(&home.display().to_string(), "<HOME>")
                    .replace(&cwd.display().to_string(), "<CWD>"),
                e.exit_code(),
            )),
        };
        let expected = &case["expected"];
        let ok = match (known_divergence(name), &actual) {
            (Some("prefix"), Err((msg, code))) => {
                let want = expected["error"].as_str().unwrap();
                let prefix = &want[..want.find(": ").unwrap() + 2];
                msg.starts_with(prefix)
                    && u64::from(*code) == expected["exit_code"].as_u64().unwrap()
            }
            (Some("first_line"), Err((msg, code))) => {
                let want = expected["error"].as_str().unwrap();
                msg == want.lines().next().unwrap()
                    && u64::from(*code) == expected["exit_code"].as_u64().unwrap()
            }
            (Some("rust_rejects"), Err(_)) => true,
            (Some(_), _) => false,
            (None, Ok(yaml)) => expected["yaml"].as_str() == Some(yaml.as_str()),
            (None, Err((msg, code))) => {
                expected["error"].as_str() == Some(msg.as_str())
                    && expected["exit_code"].as_u64() == Some(u64::from(*code))
            }
        };
        if !ok {
            failures.push(format!(
                "--- {name}\nexpected: {expected}\nactual:   {actual:?}"
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
