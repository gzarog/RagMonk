//! Python `str`/`repr` of JSON values, for messages and prompts that
//! interpolate values the way the reference's f-strings do.

use serde_json::Value;

fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn float(f: f64) -> String {
    if f.is_finite() && f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        let s = f.to_string();
        if f.is_infinite() {
            if f > 0.0 { "inf" } else { "-inf" }.into()
        } else {
            s
        }
    }
}

/// Python `repr(value)`.
pub fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => i.to_string(),
            (None, Some(u), _) => u.to_string(),
            (_, _, Some(f)) => float(f),
            _ => n.to_string(),
        },
        Value::String(s) => repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python `str(value)` (what an f-string interpolates).
pub fn str_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => repr(other),
    }
}

/// A JSON value with object keys in document order (serde_json's map
/// sorts them), for reprs of bodies the reference parsed into dicts.
enum Ordered {
    Plain(Value),
    Array(Vec<Ordered>),
    Object(Vec<(String, Ordered)>),
}

impl<'de> serde::Deserialize<'de> for Ordered {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Ordered;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON")
            }
            fn visit_bool<E>(self, v: bool) -> Result<Ordered, E> {
                Ok(Ordered::Plain(Value::Bool(v)))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Ordered, E> {
                Ok(Ordered::Plain(v.into()))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Ordered, E> {
                Ok(Ordered::Plain(v.into()))
            }
            fn visit_f64<E>(self, v: f64) -> Result<Ordered, E> {
                Ok(Ordered::Plain(v.into()))
            }
            fn visit_str<E>(self, v: &str) -> Result<Ordered, E> {
                Ok(Ordered::Plain(v.into()))
            }
            fn visit_unit<E>(self) -> Result<Ordered, E> {
                Ok(Ordered::Plain(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Ordered, A::Error> {
                let mut out = Vec::new();
                while let Some(x) = a.next_element()? {
                    out.push(x);
                }
                Ok(Ordered::Array(out))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Ordered, A::Error> {
                let mut out: Vec<(String, Ordered)> = Vec::new();
                while let Some((k, v)) = a.next_entry::<String, Ordered>()? {
                    // A repeated key keeps its first position, last value.
                    match out.iter_mut().find(|(e, _)| *e == k) {
                        Some(slot) => slot.1 = v,
                        None => out.push((k, v)),
                    }
                }
                Ok(Ordered::Object(out))
            }
        }
        d.deserialize_any(V)
    }
}

fn repr_ordered(v: &Ordered) -> String {
    match v {
        Ordered::Plain(p) => repr(p),
        Ordered::Array(a) => format!(
            "[{}]",
            a.iter().map(repr_ordered).collect::<Vec<_>>().join(", ")
        ),
        Ordered::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), repr_ordered(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python `repr(json.loads(text))`, keeping key order; `None` when
/// `text` is not JSON.
pub fn repr_json_text(text: &str) -> Option<String> {
    serde_json::from_str::<Ordered>(text)
        .ok()
        .map(|v| repr_ordered(&v))
}

/// Python's `json.loads` error message for text serde rejects, for the
/// cases the reference surfaces: a missing value (`Expecting value: line
/// L column C (char N)`); otherwise serde's own message.
pub fn json_error(text: &str, err: &serde_json::Error) -> String {
    let start = text
        .char_indices()
        .find(|(_, c)| !matches!(c, ' ' | '\t' | '\n' | '\r'));
    let at = match start {
        None => Some(text.chars().count()),
        Some((i, c)) if !matches!(c, '{' | '[' | '"' | '-' | '0'..='9' | 't' | 'f' | 'n') => {
            Some(text[..i].chars().count())
        }
        _ => None,
    };
    match at {
        Some(pos) => {
            let before: String = text.chars().take(pos).collect();
            let line = before.matches('\n').count() + 1;
            let col = match before.rfind('\n') {
                Some(nl) => before[nl + 1..].chars().count() + 1,
                None => pos + 1,
            };
            format!("Expecting value: line {line} column {col} (char {pos})")
        }
        None => err.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn reprs_like_python() {
        assert_eq!(
            repr(&json!({"a": [1, 2.0, null, true, "it's"], "b": "x\ny"})),
            r#"{'a': [1, 2.0, None, True, "it's"], 'b': 'x\ny'}"#
        );
        assert_eq!(str_of(&json!("s")), "s");
        assert_eq!(str_of(&Value::Null), "None");
        assert_eq!(
            repr_json_text(r#"{"z": 1, "a": {"y": [true], "b": null}}"#).unwrap(),
            "{'z': 1, 'a': {'y': [True], 'b': None}}"
        );
    }

    #[test]
    fn json_errors_like_python() {
        let e = serde_json::from_str::<Value>("<html>").unwrap_err();
        assert_eq!(
            json_error("<html>", &e),
            "Expecting value: line 1 column 1 (char 0)"
        );
        let e = serde_json::from_str::<Value>("").unwrap_err();
        assert_eq!(
            json_error("", &e),
            "Expecting value: line 1 column 1 (char 0)"
        );
        let e = serde_json::from_str::<Value>(" \n x").unwrap_err();
        assert_eq!(
            json_error(" \n x", &e),
            "Expecting value: line 2 column 2 (char 3)"
        );
    }
}
