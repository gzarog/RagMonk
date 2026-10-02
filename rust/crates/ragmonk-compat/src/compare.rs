//! Structural diff of two canonical captures with float tolerance.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct Difference {
    pub path: String,
    pub left: String,
    pub right: String,
}

/// Compares two values; numbers within `tolerance` (absolute) are equal.
pub fn diff(left: &Value, right: &Value, tolerance: f64) -> Vec<Difference> {
    let mut out = Vec::new();
    walk("$", left, right, tolerance, &mut out);
    out
}

fn render(value: Option<&Value>) -> String {
    match value {
        None => "<missing>".into(),
        Some(v) => {
            let text = v.to_string();
            if text.chars().count() > 200 {
                let cut: String = text.chars().take(200).collect();
                format!("{cut}…")
            } else {
                text
            }
        }
    }
}

fn walk(path: &str, left: &Value, right: &Value, tolerance: f64, out: &mut Vec<Difference>) {
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            let keys: std::collections::BTreeSet<&String> = a.keys().chain(b.keys()).collect();
            for key in keys {
                let child = format!("{path}.{key}");
                match (a.get(key), b.get(key)) {
                    (Some(x), Some(y)) => walk(&child, x, y, tolerance, out),
                    (x, y) => out.push(Difference {
                        path: child,
                        left: render(x),
                        right: render(y),
                    }),
                }
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                out.push(Difference {
                    path: format!("{path}.length"),
                    left: a.len().to_string(),
                    right: b.len().to_string(),
                });
            }
            for (i, (x, y)) in a.iter().zip(b.iter()).enumerate() {
                walk(&format!("{path}[{i}]"), x, y, tolerance, out);
            }
        }
        (Value::Number(a), Value::Number(b)) => {
            let equal = match (a.as_f64(), b.as_f64()) {
                (Some(x), Some(y)) if a.is_f64() || b.is_f64() => (x - y).abs() <= tolerance,
                _ => a == b,
            };
            if !equal {
                out.push(Difference {
                    path: path.into(),
                    left: a.to_string(),
                    right: b.to_string(),
                });
            }
        }
        (a, b) if a == b => {}
        (a, b) => out.push(Difference {
            path: path.into(),
            left: render(Some(a)),
            right: render(Some(b)),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn equal_values_have_no_diff() {
        let v = json!({"a": [1, 2.5, "x"], "b": null});
        assert!(diff(&v, &v, 0.0).is_empty());
    }

    #[test]
    fn floats_compare_with_tolerance_but_integers_exactly() {
        assert!(diff(&json!(0.1000001), &json!(0.1), 1e-6).is_empty());
        assert_eq!(diff(&json!(0.2), &json!(0.1), 1e-6).len(), 1);
        assert_eq!(diff(&json!(3), &json!(4), 10.0).len(), 1);
    }

    #[test]
    fn reports_missing_keys_and_length() {
        let d = diff(&json!({"a": [1, 2]}), &json!({"a": [1], "b": 1}), 0.0);
        let paths: Vec<_> = d.iter().map(|x| x.path.as_str()).collect();
        assert_eq!(paths, vec!["$.a.length", "$.b"]);
    }
}
