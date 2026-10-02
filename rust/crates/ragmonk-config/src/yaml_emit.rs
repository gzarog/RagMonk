//! `yaml.safe_dump(data, sort_keys=False)` for the shapes `RagMonkConfig`
//! dumps to: nested block mappings, indentless block sequences of scalars,
//! and scalars. Ported from PyYAML's `Emitter` (`analyze_scalar`,
//! `choose_scalar_style`, `write_plain`, `write_single_quoted`,
//! `write_double_quoted`) with its defaults: width 80, indent 2,
//! `allow_unicode=False`.

use crate::pyvalue::PyValue;
use crate::yaml_load::{resolve_plain, Implicit};

const BEST_WIDTH: usize = 80;
const BEST_INDENT: usize = 2;

fn is_break(c: char) -> bool {
    matches!(c, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

fn is_ws_or_end(c: Option<char>) -> bool {
    match c {
        None => true,
        Some(c) => matches!(
            c,
            '\0' | ' ' | '\t' | '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}'
        ),
    }
}

#[derive(Debug, Clone, Copy)]
struct Analysis {
    empty: bool,
    multiline: bool,
    allow_block_plain: bool,
    allow_single_quoted: bool,
}

fn analyze_scalar(scalar: &str) -> Analysis {
    let chars: Vec<char> = scalar.chars().collect();
    if chars.is_empty() {
        return Analysis {
            empty: true,
            multiline: false,
            allow_block_plain: true,
            allow_single_quoted: true,
        };
    }
    let mut block_indicators = false;
    let mut line_breaks = false;
    let mut special_characters = false;
    let mut leading_space = false;
    let mut leading_break = false;
    let mut trailing_space = false;
    let mut trailing_break = false;
    let mut break_space = false;
    let mut space_break = false;
    if scalar.starts_with("---") || scalar.starts_with("...") {
        block_indicators = true;
    }
    let mut preceded_by_whitespace = true;
    let mut followed_by_whitespace = chars.len() == 1 || is_ws_or_end(chars.get(1).copied());
    let mut previous_space = false;
    let mut previous_break = false;
    let n = chars.len();
    for (index, &ch) in chars.iter().enumerate() {
        if index == 0 {
            if "#,[]{}&*!|>'\"%@`".contains(ch) {
                block_indicators = true;
            }
            if (ch == '?' || ch == ':' || ch == '-') && followed_by_whitespace {
                block_indicators = true;
            }
        } else if (ch == ':' && followed_by_whitespace) || (ch == '#' && preceded_by_whitespace) {
            block_indicators = true;
        }
        if is_break(ch) {
            line_breaks = true;
        }
        // allow_unicode=False: every character outside printable ASCII
        // (other than '\n') is "special" and forces double quotes.
        if !(ch == '\n' || ('\x20'..='\x7e').contains(&ch)) {
            special_characters = true;
        }
        if ch == ' ' {
            if index == 0 {
                leading_space = true;
            }
            if index == n - 1 {
                trailing_space = true;
            }
            if previous_break {
                break_space = true;
            }
            previous_space = true;
            previous_break = false;
        } else if is_break(ch) {
            if index == 0 {
                leading_break = true;
            }
            if index == n - 1 {
                trailing_break = true;
            }
            if previous_space {
                space_break = true;
            }
            previous_space = false;
            previous_break = true;
        } else {
            previous_space = false;
            previous_break = false;
        }
        preceded_by_whitespace = is_ws_or_end(Some(ch));
        followed_by_whitespace = index + 2 >= n || is_ws_or_end(chars.get(index + 2).copied());
    }
    let mut allow_block_plain = true;
    let mut allow_single_quoted = true;
    if leading_space || leading_break || trailing_space || trailing_break {
        allow_block_plain = false;
    }
    if break_space {
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if space_break || special_characters {
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if line_breaks {
        allow_block_plain = false;
    }
    if block_indicators {
        allow_block_plain = false;
    }
    Analysis {
        empty: false,
        multiline: line_breaks,
        allow_block_plain,
        allow_single_quoted,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Style {
    Plain,
    Single,
    Double,
}

fn choose_style(value: &str, simple_key: bool) -> Style {
    let a = analyze_scalar(value);
    let implicit_str = resolve_plain(value) == Implicit::Str;
    // Block context only (flow_level == 0), so flow-plain rules never apply.
    if implicit_str && !(simple_key && (a.empty || a.multiline)) && a.allow_block_plain {
        return Style::Plain;
    }
    if a.allow_single_quoted && !(simple_key && a.multiline) {
        return Style::Single;
    }
    Style::Double
}

struct Writer {
    out: String,
    column: usize,
    whitespace: bool,
    indention: bool,
    indent: usize,
}

impl Writer {
    fn write(&mut self, data: &str) {
        self.column += data.chars().count();
        self.out.push_str(data);
    }

    fn line_break(&mut self, data: Option<char>) {
        self.out.push(data.unwrap_or('\n'));
        self.whitespace = true;
        self.indention = true;
        self.column = 0;
    }

    fn write_indent(&mut self) {
        let indent = self.indent;
        if !self.indention || self.column > indent || (self.column == indent && !self.whitespace) {
            self.line_break(None);
        }
        if self.column < indent {
            self.whitespace = true;
            let pad = " ".repeat(indent - self.column);
            self.out.push_str(&pad);
            self.column = indent;
        }
    }

    fn write_indicator(
        &mut self,
        indicator: &str,
        need_whitespace: bool,
        whitespace: bool,
        indention: bool,
    ) {
        let data = if self.whitespace || !need_whitespace {
            indicator.to_owned()
        } else {
            format!(" {indicator}")
        };
        self.whitespace = whitespace;
        self.indention = self.indention && indention;
        self.write(&data);
    }

    fn write_plain(&mut self, text: &[char], split: bool) {
        if text.is_empty() {
            return;
        }
        if !self.whitespace {
            self.write(" ");
        }
        self.whitespace = false;
        self.indention = false;
        let mut spaces = false;
        let mut breaks = false;
        let (mut start, mut end) = (0usize, 0usize);
        while end <= text.len() {
            let ch = text.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end && self.column > BEST_WIDTH && split {
                        self.write_indent();
                        self.whitespace = false;
                        self.indention = false;
                    } else {
                        let data: String = text[start..end].iter().collect();
                        self.write(&data);
                    }
                    start = end;
                }
            } else if breaks {
                if !ch.is_some_and(is_break) {
                    if text[start] == '\n' {
                        self.line_break(None);
                    }
                    for &br in &text[start..end] {
                        self.line_break(if br == '\n' { None } else { Some(br) });
                    }
                    self.write_indent();
                    self.whitespace = false;
                    self.indention = false;
                    start = end;
                }
            } else if ch.is_none_or(|c| c == ' ' || is_break(c)) {
                let data: String = text[start..end].iter().collect();
                self.write(&data);
                start = end;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
                breaks = is_break(c);
            }
            end += 1;
        }
    }

    fn write_single_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("'", true, false, false);
        let mut spaces = false;
        let mut breaks = false;
        let (mut start, mut end) = (0usize, 0usize);
        while end <= text.len() {
            let ch = text.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > BEST_WIDTH
                        && split
                        && start != 0
                        && end != text.len()
                    {
                        self.write_indent();
                    } else {
                        let data: String = text[start..end].iter().collect();
                        self.write(&data);
                    }
                    start = end;
                }
            } else if breaks {
                if !ch.is_some_and(is_break) {
                    if text[start] == '\n' {
                        self.line_break(None);
                    }
                    for &br in &text[start..end] {
                        self.line_break(if br == '\n' { None } else { Some(br) });
                    }
                    self.write_indent();
                    start = end;
                }
            } else if ch.is_none_or(|c| c == ' ' || is_break(c) || c == '\'') && start < end {
                let data: String = text[start..end].iter().collect();
                self.write(&data);
                start = end;
            }
            if ch == Some('\'') {
                self.write("''");
                start = end + 1;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
                breaks = is_break(c);
            }
            end += 1;
        }
        self.write_indicator("'", false, false, false);
    }

    fn write_double_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("\"", true, false, false);
        let (mut start, mut end) = (0usize, 0usize);
        while end <= text.len() {
            let ch = text.get(end).copied();
            let needs_escape = match ch {
                None => true,
                Some(c) => {
                    "\"\\\u{85}\u{2028}\u{2029}\u{feff}".contains(c)
                        || !('\x20'..='\x7e').contains(&c)
                }
            };
            if needs_escape {
                if start < end {
                    let data: String = text[start..end].iter().collect();
                    self.write(&data);
                    start = end;
                }
                if let Some(c) = ch {
                    let data = match escape_replacement(c) {
                        Some(r) => format!("\\{r}"),
                        None if (c as u32) <= 0xff => format!("\\x{:02X}", c as u32),
                        None if (c as u32) <= 0xffff => format!("\\u{:04X}", c as u32),
                        None => format!("\\U{:08X}", c as u32),
                    };
                    self.write(&data);
                    start = end + 1;
                }
            }
            if 0 < end
                && end + 1 < text.len()
                && (ch == Some(' ') || start >= end)
                && self.column + end.saturating_sub(start) > BEST_WIDTH
                && split
            {
                let mut data: String = if start < end {
                    text[start..end].iter().collect()
                } else {
                    String::new()
                };
                data.push('\\');
                if start < end {
                    start = end;
                }
                self.write(&data);
                self.write_indent();
                self.whitespace = false;
                self.indention = false;
                if text[start] == ' ' {
                    self.write("\\");
                }
            }
            end += 1;
        }
        self.write_indicator("\"", false, false, false);
    }

    fn scalar(&mut self, text: &str, is_str: bool, simple_key: bool) {
        let chars: Vec<char> = text.chars().collect();
        let style = if is_str {
            choose_style(text, simple_key)
        } else {
            Style::Plain
        };
        let split = !simple_key;
        match style {
            Style::Plain => self.write_plain(&chars, split),
            Style::Single => self.write_single_quoted(&chars, split),
            Style::Double => self.write_double_quoted(&chars, split),
        }
    }
}

fn escape_replacement(c: char) -> Option<char> {
    Some(match c {
        '\0' => '0',
        '\x07' => 'a',
        '\x08' => 'b',
        '\x09' => 't',
        '\x0a' => 'n',
        '\x0b' => 'v',
        '\x0c' => 'f',
        '\x0d' => 'r',
        '\x1b' => 'e',
        '"' => '"',
        '\\' => '\\',
        '\u{85}' => 'N',
        '\u{a0}' => '_',
        '\u{2028}' => 'L',
        '\u{2029}' => 'P',
        _ => return None,
    })
}

/// PyYAML `represent_float`.
pub fn represent_float(v: f64) -> String {
    if v.is_nan() {
        return ".nan".into();
    }
    if v == f64::INFINITY {
        return ".inf".into();
    }
    if v == f64::NEG_INFINITY {
        return "-.inf".into();
    }
    let mut value = ragmonk_telemetry::logging::python_float_repr(v).to_lowercase();
    if !value.contains('.') && value.contains('e') {
        value = value.replacen('e', ".0e", 1);
    }
    value
}

/// Text of a non-collection value plus whether it is a `str`.
fn scalar_text(v: &PyValue) -> Option<(String, bool)> {
    Some(match v {
        PyValue::None => ("null".into(), false),
        PyValue::Bool(b) => (if *b { "true" } else { "false" }.into(), false),
        PyValue::Int(i) => (i.to_string(), false),
        PyValue::Float(f) => (represent_float(*f), false),
        PyValue::Str(s) => (s.clone(), true),
        PyValue::Timestamp(s) => (s.clone(), false),
        PyValue::Bytes(_) | PyValue::List(_) | PyValue::Dict(_) => return None,
    })
}

/// Emits a top-level mapping. Values that are lists must contain scalars
/// and be non-empty; mappings must be non-empty (true for every config).
pub fn safe_dump(data: &PyValue) -> String {
    let mut w = Writer {
        out: String::new(),
        column: 0,
        whitespace: true,
        indention: true,
        indent: 0,
    };
    if let PyValue::Dict(entries) = data {
        emit_mapping(&mut w, entries, 0);
    }
    w.line_break(None);
    w.out
}

fn emit_mapping(w: &mut Writer, entries: &[(PyValue, PyValue)], indent: usize) {
    for (key, value) in entries {
        w.indent = indent;
        w.write_indent();
        let (key_text, key_is_str) = scalar_text(key).unwrap_or_default();
        w.indent = indent + BEST_INDENT;
        w.scalar(&key_text, key_is_str, true);
        w.write_indicator(":", false, false, false);
        match value {
            PyValue::Dict(sub) if !sub.is_empty() => emit_mapping(w, sub, indent + BEST_INDENT),
            PyValue::List(items) if !items.is_empty() => {
                for item in items {
                    w.indent = indent;
                    w.write_indent();
                    w.write_indicator("-", true, false, true);
                    w.indent = indent + BEST_INDENT;
                    let (text, is_str) = scalar_text(item).unwrap_or_default();
                    w.scalar(&text, is_str, false);
                }
            }
            PyValue::Dict(_) => w.write(" {}"),
            PyValue::List(_) => w.write(" []"),
            other => {
                w.indent = indent + BEST_INDENT;
                let (text, is_str) = scalar_text(other).unwrap_or_default();
                w.scalar(&text, is_str, false);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_like_pyyaml() {
        assert_eq!(represent_float(30.0), "30.0");
        assert_eq!(represent_float(1e16), "1.0e+16");
        assert_eq!(represent_float(1e-5), "1.0e-05");
        assert_eq!(represent_float(f64::NEG_INFINITY), "-.inf");
    }

    #[test]
    fn styles() {
        assert_eq!(choose_style("info", false), Style::Plain);
        assert_eq!(choose_style("", false), Style::Single);
        assert_eq!(choose_style("on", false), Style::Single);
        assert_eq!(choose_style("a\tb", false), Style::Double);
        assert_eq!(choose_style("é", false), Style::Double);
        assert_eq!(choose_style("a, b", false), Style::Plain);
    }
}
