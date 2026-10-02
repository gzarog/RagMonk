//! The subset of Python objects a `yaml.safe_load` can produce.

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum PyValue {
    None,
    Bool(bool),
    /// Python ints are unbounded; values beyond i128 are not representable.
    Int(i128),
    Float(f64),
    Str(String),
    /// `datetime.date`/`datetime` from an implicit YAML timestamp.
    Timestamp(String),
    Bytes(Vec<u8>),
    List(Vec<PyValue>),
    /// Insertion-ordered dict; a repeated key keeps its first position and
    /// takes the last value, like Python.
    Dict(Vec<(PyValue, PyValue)>),
}

impl PyValue {
    pub fn str(s: impl Into<String>) -> Self {
        PyValue::Str(s.into())
    }

    pub fn as_dict(&self) -> Option<&Vec<(PyValue, PyValue)>> {
        match self {
            PyValue::Dict(d) => Some(d),
            _ => None,
        }
    }

    /// Python dict key equality (`1 == 1.0 == True`).
    pub fn key_eq(&self, other: &PyValue) -> bool {
        match (self.as_number(), other.as_number()) {
            (Some(a), Some(b)) => a == b,
            _ => self == other,
        }
    }

    fn as_number(&self) -> Option<f64> {
        match self {
            PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
            PyValue::Int(i) => Some(*i as f64),
            PyValue::Float(f) => Some(*f),
            _ => None,
        }
    }

    pub fn get(&self, key: &str) -> Option<&PyValue> {
        self.as_dict()?
            .iter()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if s == key))
            .map(|(_, v)| v)
    }
}

/// Inserts/replaces like `dict[key] = value`.
pub fn dict_set(dict: &mut Vec<(PyValue, PyValue)>, key: PyValue, value: PyValue) {
    if let Some(slot) = dict.iter_mut().find(|(k, _)| k.key_eq(&key)) {
        slot.1 = value;
    } else {
        dict.push((key, value));
    }
}

/// Python `repr(str)`.
pub fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
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
            c if is_printable(c) => out.push(c),
            c if (c as u32) <= 0xff => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if (c as u32) <= 0xffff => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => {
                let _ = write!(out, "\\U{:08x}", c as u32);
            }
        }
    }
    out.push(quote);
    out
}

/// Approximates Python's `str.isprintable` (Unicode categories Cc, Cf, Cs,
/// Co, Cn, Zl, Zp, Zs except ASCII space are non-printable).
fn is_printable(c: char) -> bool {
    if c == ' ' {
        return true;
    }
    if c.is_control() || c.is_whitespace() {
        return false;
    }
    !matches!(c as u32,
        0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x180e | 0x200b..=0x200f
        | 0x202a..=0x202e | 0x2060..=0x2064 | 0x2066..=0x206f | 0xfeff | 0xfff9..=0xfffb
        | 0xe000..=0xf8ff | 0xf0000..=0x10ffff)
}

/// Python `repr(list[str])`, e.g. `['a', 'b']`.
pub fn py_repr_str_list(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|s| py_repr_str(s)).collect();
    format!("[{}]", inner.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_matches_python() {
        assert_eq!(py_repr_str("semantic"), "'semantic'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_str("a'\""), "'a\\'\"'");
        assert_eq!(py_repr_str("a\tb\n"), "'a\\tb\\n'");
        assert_eq!(py_repr_str("é\u{1}"), "'é\\x01'");
        assert_eq!(py_repr_str_list(&["always", "auto"]), "['always', 'auto']");
    }

    #[test]
    fn dict_semantics() {
        let mut d = Vec::new();
        dict_set(&mut d, PyValue::Int(1), PyValue::str("a"));
        dict_set(&mut d, PyValue::Bool(true), PyValue::str("b"));
        dict_set(&mut d, PyValue::str("x"), PyValue::Int(2));
        assert_eq!(d.len(), 2);
        assert_eq!(d[0], (PyValue::Int(1), PyValue::str("b")));
    }
}
