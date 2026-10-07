//! PyYAML `yaml.safe_load` semantics on top of the `yaml-rust2` event
//! parser. `yaml-rust2` resolves plain scalars with YAML 1.2 rules, so this
//! module re-resolves raw plain scalars with PyYAML's YAML 1.1 implicit
//! resolvers and constructors (`yaml/resolver.py`, `yaml/constructor.py`).
//!
//! Known divergence: syntax-error *messages* differ from PyYAML's text (the
//! `failed to read config file <path>: ` prefix and exit code match).

use std::collections::HashMap;
use std::sync::OnceLock;

use regex::Regex;
use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser, Tag};
use yaml_rust2::scanner::{Marker, TScalarStyle};

use crate::pyvalue::{dict_set, PyValue};

const YAML_NS: &str = "tag:yaml.org,2002:";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct YamlError(pub String);

impl std::fmt::Display for YamlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// The YAML 1.1 type a plain scalar implicitly resolves to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Implicit {
    Bool,
    Float,
    Int,
    Merge,
    Null,
    Timestamp,
    Value,
    Str,
}

struct Resolvers {
    bool_: Regex,
    float: Regex,
    int: Regex,
    merge: Regex,
    null: Regex,
    timestamp: Regex,
    value: Regex,
}

fn resolvers() -> &'static Resolvers {
    static R: OnceLock<Resolvers> = OnceLock::new();
    R.get_or_init(|| {
        let re = |p: &str| Regex::new(p).expect("static regex");
        Resolvers {
            bool_: re(
                r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
            ),
            float: re(concat!(
                r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?",
                r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?",
                r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*",
                r"|[-+]?\.(?:inf|Inf|INF)",
                r"|\.(?:nan|NaN|NAN))$"
            )),
            int: re(concat!(
                r"^(?:[-+]?0b[0-1_]+",
                r"|[-+]?0[0-7_]+",
                r"|[-+]?(?:0|[1-9][0-9_]*)",
                r"|[-+]?0x[0-9a-fA-F_]+",
                r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$"
            )),
            merge: re(r"^(?:<<)$"),
            null: re(r"^(?:~|null|Null|NULL|)$"),
            timestamp: re(concat!(
                r"^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
                r"|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?",
                r"(?:[Tt]|[ \t]+)[0-9][0-9]?",
                r":[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?",
                r"(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$"
            )),
            value: re(r"^(?:=)$"),
        }
    })
}

/// PyYAML's implicit resolution of a plain scalar. Resolvers are keyed by
/// the first character and tried in registration order.
pub fn resolve_plain(value: &str) -> Implicit {
    let r = resolvers();
    let first = value.chars().next();
    let candidates: &[(Implicit, &Regex)] = match first {
        None => &[(Implicit::Null, &r.null)],
        Some(c) => match c {
            'y' | 'Y' | 'n' | 'N' | 't' | 'T' | 'f' | 'F' | 'o' | 'O' => {
                return if r.bool_.is_match(value) {
                    Implicit::Bool
                } else if matches!(c, 'n' | 'N') && r.null.is_match(value) {
                    Implicit::Null
                } else {
                    Implicit::Str
                };
            }
            '-' | '+' | '0'..='9' => {
                for (kind, re) in [
                    (Implicit::Float, &r.float),
                    (Implicit::Int, &r.int),
                    (Implicit::Timestamp, &r.timestamp),
                ] {
                    if re.is_match(value) {
                        return kind;
                    }
                }
                return Implicit::Str;
            }
            '.' => &[(Implicit::Float, &r.float)],
            '<' => &[(Implicit::Merge, &r.merge)],
            '~' => &[(Implicit::Null, &r.null)],
            '=' => &[(Implicit::Value, &r.value)],
            _ => &[],
        },
    };
    candidates
        .iter()
        .find(|(_, re)| re.is_match(value))
        .map(|(k, _)| *k)
        .unwrap_or(Implicit::Str)
}

fn construct_bool(v: &str) -> Option<bool> {
    match v.to_ascii_lowercase().as_str() {
        "yes" | "true" | "on" => Some(true),
        "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

fn construct_int(raw: &str) -> Result<i128, YamlError> {
    let err = || YamlError(format!("invalid literal for int(): {raw:?}"));
    let mut v: String = raw.chars().filter(|&c| c != '_').collect();
    let mut sign: i128 = 1;
    if let Some(rest) = v.strip_prefix('-') {
        sign = -1;
        v = rest.to_owned();
    } else if let Some(rest) = v.strip_prefix('+') {
        v = rest.to_owned();
    }
    let value = if v == "0" {
        0
    } else if let Some(b) = v.strip_prefix("0b") {
        i128::from_str_radix(b, 2).map_err(|_| err())?
    } else if let Some(h) = v.strip_prefix("0x") {
        i128::from_str_radix(h, 16).map_err(|_| err())?
    } else if v.starts_with('0') {
        i128::from_str_radix(&v, 8).map_err(|_| err())?
    } else if v.contains(':') {
        let mut acc: i128 = 0;
        for part in v.split(':') {
            acc = acc
                .checked_mul(60)
                .and_then(|a| a.checked_add(part.parse::<i128>().ok()?))
                .ok_or_else(err)?;
        }
        acc
    } else {
        v.parse::<i128>().map_err(|_| err())?
    };
    Ok(sign * value)
}

fn construct_float(raw: &str) -> Result<f64, YamlError> {
    let err = || YamlError(format!("could not convert string to float: {raw:?}"));
    let mut v: String = raw
        .chars()
        .filter(|&c| c != '_')
        .collect::<String>()
        .to_lowercase();
    let mut sign = 1.0;
    if let Some(rest) = v.strip_prefix('-') {
        sign = -1.0;
        v = rest.to_owned();
    } else if let Some(rest) = v.strip_prefix('+') {
        v = rest.to_owned();
    }
    if v == ".inf" {
        return Ok(sign * f64::INFINITY);
    }
    if v == ".nan" {
        return Ok(f64::NAN);
    }
    if v.contains(':') {
        let mut acc = 0.0;
        let mut base = 1.0;
        for part in v.split(':').rev() {
            acc += part.parse::<f64>().map_err(|_| err())? * base;
            base *= 60.0;
        }
        return Ok(sign * acc);
    }
    // "1." and ".5" are valid YAML 1.1 floats; Rust's parser accepts both.
    Ok(sign * v.parse::<f64>().map_err(|_| err())?)
}

fn base64_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut buf = 0u32;
    let mut bits = 0;
    for c in s.chars().filter(|c| !c.is_whitespace()) {
        let v = match c {
            'A'..='Z' => c as u32 - 'A' as u32,
            'a'..='z' => c as u32 - 'a' as u32 + 26,
            '0'..='9' => c as u32 - '0' as u32 + 52,
            '+' => 62,
            '/' => 63,
            '=' => break,
            _ => return None,
        };
        buf = (buf << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
            buf &= (1 << bits) - 1;
        }
    }
    Some(out)
}

fn construct_scalar(
    value: String,
    style: TScalarStyle,
    tag: Option<&Tag>,
) -> Result<PyValue, YamlError> {
    let kind = match tag {
        Some(t) => {
            let full = format!("{}{}", t.handle, t.suffix);
            match full.strip_prefix(YAML_NS) {
                Some("str") => Implicit::Str,
                Some("int") => Implicit::Int,
                Some("float") => Implicit::Float,
                Some("bool") => Implicit::Bool,
                Some("null") => Implicit::Null,
                Some("timestamp") => Implicit::Timestamp,
                Some("binary") => {
                    return base64_decode(&value)
                        .map(PyValue::Bytes)
                        .ok_or_else(|| YamlError("failed to decode base64 data".into()));
                }
                _ if full == "!" => Implicit::Str,
                _ => {
                    return Err(YamlError(format!(
                        "could not determine a constructor for the tag {}",
                        crate::pyvalue::py_repr_str(&full)
                    )))
                }
            }
        }
        None if style == TScalarStyle::Plain => resolve_plain(&value),
        None => Implicit::Str,
    };
    Ok(match kind {
        Implicit::Str => PyValue::Str(value),
        Implicit::Null => PyValue::None,
        Implicit::Bool => PyValue::Bool(
            construct_bool(&value).ok_or_else(|| YamlError(format!("invalid bool {value:?}")))?,
        ),
        Implicit::Int => PyValue::Int(construct_int(&value)?),
        Implicit::Float => PyValue::Float(construct_float(&value)?),
        Implicit::Timestamp => PyValue::Timestamp(value),
        Implicit::Merge => PyValue::Str("<<".into()),
        Implicit::Value => {
            return Err(YamlError(
                "could not determine a constructor for the tag 'tag:yaml.org,2002:value'".into(),
            ))
        }
    })
}

/// A node before merge-key flattening. `is_merge_key` marks a plain `<<`.
#[derive(Debug, Clone)]
enum Node {
    Scalar(PyValue, bool),
    Seq(Vec<Node>),
    Map(Vec<(Node, Node)>),
}

enum Frame {
    Seq(usize, Vec<Node>),
    Map(usize, Vec<(Node, Node)>, Option<Node>),
}

#[derive(Default)]
struct Builder {
    stack: Vec<Frame>,
    anchors: HashMap<usize, Node>,
    docs: Vec<Node>,
    error: Option<YamlError>,
}

impl Builder {
    fn push_node(&mut self, node: Node, anchor: usize) {
        if anchor > 0 {
            self.anchors.insert(anchor, node.clone());
        }
        match self.stack.last_mut() {
            Some(Frame::Seq(_, items)) => items.push(node),
            Some(Frame::Map(_, entries, pending)) => match pending.take() {
                Some(key) => entries.push((key, node)),
                None => *pending = Some(node),
            },
            None => self.docs.push(node),
        }
    }
}

impl MarkedEventReceiver for Builder {
    fn on_event(&mut self, ev: Event, _mark: Marker) {
        if self.error.is_some() {
            return;
        }
        match ev {
            Event::Scalar(value, style, anchor, tag) => {
                let is_merge = tag.is_none() && style == TScalarStyle::Plain && value == "<<";
                match construct_scalar(value, style, tag.as_ref()) {
                    Ok(v) => self.push_node(Node::Scalar(v, is_merge), anchor),
                    Err(e) => self.error = Some(e),
                }
            }
            Event::SequenceStart(anchor, tag) => {
                if let Some(e) = check_collection_tag(tag.as_ref(), "seq") {
                    self.error = Some(e);
                    return;
                }
                self.stack.push(Frame::Seq(anchor, Vec::new()));
            }
            Event::MappingStart(anchor, tag) => {
                if let Some(e) = check_collection_tag(tag.as_ref(), "map") {
                    self.error = Some(e);
                    return;
                }
                self.stack.push(Frame::Map(anchor, Vec::new(), None));
            }
            Event::SequenceEnd => {
                if let Some(Frame::Seq(anchor, items)) = self.stack.pop() {
                    self.push_node(Node::Seq(items), anchor);
                }
            }
            Event::MappingEnd => {
                if let Some(Frame::Map(anchor, entries, _)) = self.stack.pop() {
                    self.push_node(Node::Map(entries), anchor);
                }
            }
            Event::Alias(id) => match self.anchors.get(&id).cloned() {
                Some(node) => self.push_node(node, 0),
                None => self.error = Some(YamlError(format!("found undefined alias {id}"))),
            },
            _ => {}
        }
    }
}

fn check_collection_tag(tag: Option<&Tag>, expected: &str) -> Option<YamlError> {
    let tag = tag?;
    let full = format!("{}{}", tag.handle, tag.suffix);
    if full == format!("{YAML_NS}{expected}") || full == "!" {
        return None;
    }
    Some(YamlError(format!(
        "could not determine a constructor for the tag {}",
        crate::pyvalue::py_repr_str(&full)
    )))
}

/// PyYAML `flatten_mapping` + `construct_mapping`.
fn to_py(node: &Node) -> Result<PyValue, YamlError> {
    match node {
        Node::Scalar(v, _) => Ok(v.clone()),
        Node::Seq(items) => Ok(PyValue::List(
            items.iter().map(to_py).collect::<Result<_, _>>()?,
        )),
        Node::Map(entries) => {
            let flat = flatten(entries)?;
            let mut dict = Vec::new();
            for (k, v) in &flat {
                let key = to_py(k)?;
                if matches!(key, PyValue::List(_) | PyValue::Dict(_)) {
                    return Err(YamlError("found unhashable key".into()));
                }
                dict_set(&mut dict, key, to_py(v)?);
            }
            Ok(PyValue::Dict(dict))
        }
    }
}

fn flatten(entries: &[(Node, Node)]) -> Result<Vec<(Node, Node)>, YamlError> {
    let mut merge: Vec<(Node, Node)> = Vec::new();
    let mut rest = Vec::new();
    for (k, v) in entries {
        if matches!(k, Node::Scalar(_, true)) {
            match v {
                Node::Map(sub) => merge.extend(flatten(sub)?),
                Node::Seq(items) => {
                    let mut submerge = Vec::new();
                    for item in items {
                        match item {
                            Node::Map(sub) => submerge.push(flatten(sub)?),
                            _ => return Err(YamlError("expected a mapping for merging".into())),
                        }
                    }
                    for sub in submerge.into_iter().rev() {
                        merge.extend(sub);
                    }
                }
                _ => {
                    return Err(YamlError(
                        "expected a mapping or list of mappings for merging".into(),
                    ))
                }
            }
        } else {
            rest.push((k.clone(), v.clone()));
        }
    }
    merge.extend(rest);
    Ok(merge)
}

/// `yaml.safe_load(text)`: `None` for an empty stream; an error for a
/// syntax error, unknown tag or more than one document.
pub fn safe_load(text: &str) -> Result<PyValue, YamlError> {
    let mut builder = Builder::default();
    let mut parser = Parser::new_from_str(text);
    parser
        .load(&mut builder, true)
        .map_err(|e| YamlError(e.to_string()))?;
    if let Some(e) = builder.error {
        return Err(e);
    }
    match builder.docs.len() {
        0 => Ok(PyValue::None),
        1 => to_py(&builder.docs[0]),
        _ => Err(YamlError("expected a single document in the stream".into())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yaml11_resolution() {
        assert_eq!(resolve_plain("yes"), Implicit::Bool);
        assert_eq!(resolve_plain("tRUE"), Implicit::Str);
        assert_eq!(resolve_plain("012"), Implicit::Int);
        assert_eq!(resolve_plain("1:30"), Implicit::Int);
        assert_eq!(resolve_plain("1:30.5"), Implicit::Float);
        assert_eq!(resolve_plain("1e5"), Implicit::Str);
        assert_eq!(resolve_plain("1.0e+5"), Implicit::Float);
        assert_eq!(resolve_plain("2024-01-02"), Implicit::Timestamp);
        assert_eq!(resolve_plain(""), Implicit::Null);
        assert_eq!(resolve_plain("Null"), Implicit::Null);
        assert_eq!(resolve_plain("<<"), Implicit::Merge);
        assert_eq!(resolve_plain("1.0e+16"), Implicit::Float);
    }

    #[test]
    fn constructs_like_pyyaml() {
        let v =
            safe_load("a: 012\nb: 0x1A\nc: 0b101\nd: 1:30\ne: 1_024\nf: on\ng: ~\nh:\n").unwrap();
        assert_eq!(v.get("a"), Some(&PyValue::Int(10)));
        assert_eq!(v.get("b"), Some(&PyValue::Int(26)));
        assert_eq!(v.get("c"), Some(&PyValue::Int(5)));
        assert_eq!(v.get("d"), Some(&PyValue::Int(90)));
        assert_eq!(v.get("e"), Some(&PyValue::Int(1024)));
        assert_eq!(v.get("f"), Some(&PyValue::Bool(true)));
        assert_eq!(v.get("g"), Some(&PyValue::None));
        assert_eq!(v.get("h"), Some(&PyValue::None));
    }

    #[test]
    fn merge_keys_and_duplicates() {
        let v = safe_load("b: &b {x: 1, y: 2}\nm:\n  <<: *b\n  y: 3\n  y: 4\n").unwrap();
        let m = v.get("m").unwrap().as_dict().unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!(v.get("m").unwrap().get("x"), Some(&PyValue::Int(1)));
        assert_eq!(v.get("m").unwrap().get("y"), Some(&PyValue::Int(4)));
    }

    #[test]
    fn errors() {
        assert!(safe_load("a: [\n").is_err());
        assert!(safe_load("a: !foo x\n").is_err());
        assert!(safe_load("a: =\n").is_err());
        assert!(safe_load("a\n---\nb\n").is_err());
        assert_eq!(safe_load("").unwrap(), PyValue::None);
    }
}
