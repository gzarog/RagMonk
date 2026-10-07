//! pydantic v2 lax-mode coercion for the field types `RagMonkConfig` uses,
//! with pydantic's exact error messages, plus Python `int()`/`float()`
//! string parsing used by the env-var layer.

use crate::pyvalue::PyValue;

pub const MSG_INT_TYPE: &str = "Input should be a valid integer";
pub const MSG_INT_PARSING: &str =
    "Input should be a valid integer, unable to parse string as an integer";
pub const MSG_INT_FROM_FLOAT: &str =
    "Input should be a valid integer, got a number with a fractional part";
pub const MSG_INT_SIZE: &str = "Unable to parse input string as an integer, exceeded maximum size";
pub const MSG_FINITE: &str = "Input should be a finite number";
/// Rust-only: Python ints are unbounded, config ints here are i64.
pub const MSG_INT_RANGE: &str =
    "Input should be a valid integer, exceeded the supported 64-bit integer range";
pub const MSG_FLOAT_TYPE: &str = "Input should be a valid number";
pub const MSG_FLOAT_PARSING: &str =
    "Input should be a valid number, unable to parse string as a number";
pub const MSG_BOOL_TYPE: &str = "Input should be a valid boolean";
pub const MSG_BOOL_PARSING: &str = "Input should be a valid boolean, unable to interpret input";
pub const MSG_STR_TYPE: &str = "Input should be a valid string";
pub const MSG_LIST_TYPE: &str = "Input should be a valid list";

/// pydantic strips ASCII whitespace (`str.strip()` semantics for the
/// characters pydantic-core trims).
fn strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace())
}

/// Python `int(str)` digits: optional sign, ASCII digits with single `_`
/// separators. Returns `None` on any syntax error.
pub fn parse_python_int(raw: &str) -> Option<i128> {
    let s = strip(raw);
    let (neg, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    if !valid_digit_groups(digits) {
        return None;
    }
    let clean: String = digits.chars().filter(|&c| c != '_').collect();
    let value: i128 = clean.parse().ok()?;
    Some(if neg { -value } else { value })
}

fn valid_digit_groups(s: &str) -> bool {
    !s.is_empty()
        && !s.starts_with('_')
        && !s.ends_with('_')
        && !s.contains("__")
        && s.chars().all(|c| c.is_ascii_digit() || c == '_')
}

/// Python `float(str)`.
pub fn parse_python_float(raw: &str) -> Option<f64> {
    let s = strip(raw);
    let (sign, body) = match s.as_bytes().first() {
        Some(b'-') => (-1.0, &s[1..]),
        Some(b'+') => (1.0, &s[1..]),
        _ => (1.0, s),
    };
    let lower = body.to_ascii_lowercase();
    match lower.as_str() {
        "inf" | "infinity" => return Some(sign * f64::INFINITY),
        "nan" => return Some(f64::NAN),
        _ => {}
    }
    let (mantissa, exponent) = match lower.find('e') {
        Some(i) => (&lower[..i], Some(&lower[i + 1..])),
        None => (lower.as_str(), None),
    };
    let (int_part, frac_part) = match mantissa.split_once('.') {
        Some((a, b)) => (a, Some(b)),
        None => (mantissa, None),
    };
    let int_ok = int_part.is_empty() || valid_digit_groups(int_part);
    let frac_ok = frac_part.is_none_or(|f| f.is_empty() || valid_digit_groups(f));
    let has_digits = !int_part.is_empty() || frac_part.is_some_and(|f| !f.is_empty());
    if !(int_ok && frac_ok && has_digits) {
        return None;
    }
    if let Some(exp) = exponent {
        let e = exp.strip_prefix(['+', '-']).unwrap_or(exp);
        if !valid_digit_groups(e) {
            return None;
        }
    }
    let text = lower.replace('_', "");
    let v: f64 = text.parse().ok()?;
    Some(sign * v)
}

pub type Coerced<T> = Result<T, &'static str>;

fn int_from_f64(f: f64) -> Coerced<i64> {
    if !f.is_finite() {
        return Err(MSG_FINITE);
    }
    if f.fract() != 0.0 {
        return Err(MSG_INT_FROM_FLOAT);
    }
    if f.abs() >= 9.223_372_036_854_776e18 {
        return Err(MSG_INT_SIZE);
    }
    Ok(f as i64)
}

fn i128_to_i64(v: i128) -> Coerced<i64> {
    i64::try_from(v).map_err(|_| MSG_INT_RANGE)
}

/// Lax `int`.
pub fn to_int(v: &PyValue) -> Coerced<i64> {
    match v {
        PyValue::Bool(b) => Ok(i64::from(*b)),
        PyValue::Int(i) => i128_to_i64(*i),
        PyValue::Float(f) => int_from_f64(*f),
        PyValue::Str(s) => {
            if let Some(i) = parse_python_int(s) {
                return i128_to_i64(i);
            }
            // pydantic also accepts a float-looking string with an
            // all-zero fractional part ("5.0", " 5.000 ").
            let t = strip(s);
            if let Some((int_part, frac)) = t.split_once('.') {
                if !frac.is_empty() && frac.chars().all(|c| c == '0') {
                    if let Some(i) = parse_python_int(int_part) {
                        return i128_to_i64(i);
                    }
                }
            }
            Err(MSG_INT_PARSING)
        }
        _ => Err(MSG_INT_TYPE),
    }
}

/// Lax `float`.
pub fn to_float(v: &PyValue) -> Coerced<f64> {
    match v {
        PyValue::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        PyValue::Int(i) => Ok(*i as f64),
        PyValue::Float(f) => Ok(*f),
        PyValue::Str(s) => parse_python_float(s).ok_or(MSG_FLOAT_PARSING),
        _ => Err(MSG_FLOAT_TYPE),
    }
}

/// Lax `bool`.
pub fn to_bool(v: &PyValue) -> Coerced<bool> {
    match v {
        PyValue::Bool(b) => Ok(*b),
        PyValue::Int(0) => Ok(false),
        PyValue::Int(1) => Ok(true),
        PyValue::Int(_) => Err(MSG_BOOL_PARSING),
        PyValue::Float(f) if *f == 0.0 => Ok(false),
        PyValue::Float(f) if *f == 1.0 => Ok(true),
        PyValue::Str(s) => match s.to_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Ok(false),
            "1" | "on" | "t" | "true" | "y" | "yes" => Ok(true),
            _ => Err(MSG_BOOL_PARSING),
        },
        _ => Err(MSG_BOOL_TYPE),
    }
}

/// `str` (pydantic never coerces numbers/bools to strings).
pub fn to_str(v: &PyValue) -> Coerced<String> {
    match v {
        PyValue::Str(s) => Ok(s.clone()),
        _ => Err(MSG_STR_TYPE),
    }
}

/// `str | None`.
pub fn to_opt_str(v: &PyValue) -> Coerced<Option<String>> {
    match v {
        PyValue::None => Ok(None),
        other => to_str(other).map(Some),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_parsing() {
        assert_eq!(parse_python_int(" +1_000 "), Some(1000));
        assert_eq!(parse_python_int("-0"), Some(0));
        for bad in ["1__0", "_1", "1_", "0x10", "", "5.0", "1e3"] {
            assert_eq!(parse_python_int(bad), None, "{bad}");
        }
    }

    #[test]
    fn python_float_parsing() {
        assert_eq!(parse_python_float("1_000.5"), Some(1000.5));
        assert_eq!(parse_python_float(".5"), Some(0.5));
        assert_eq!(parse_python_float("5."), Some(5.0));
        assert_eq!(parse_python_float("-Infinity"), Some(f64::NEG_INFINITY));
        assert!(parse_python_float(" nan ").unwrap().is_nan());
        assert_eq!(parse_python_float("1e-1"), Some(0.1));
        for bad in ["1e", "", ".", "1_", "e5", "1.2.3", "inf1"] {
            assert_eq!(parse_python_float(bad), None, "{bad}");
        }
    }

    #[test]
    fn lax_int() {
        assert_eq!(to_int(&PyValue::str(" 5.000 ")), Ok(5));
        assert_eq!(to_int(&PyValue::str("5.5")), Err(MSG_INT_PARSING));
        assert_eq!(to_int(&PyValue::Float(5.5)), Err(MSG_INT_FROM_FLOAT));
        assert_eq!(to_int(&PyValue::Float(1e20)), Err(MSG_INT_SIZE));
        assert_eq!(to_int(&PyValue::Float(f64::INFINITY)), Err(MSG_FINITE));
        assert_eq!(to_int(&PyValue::Bool(true)), Ok(1));
        assert_eq!(to_int(&PyValue::None), Err(MSG_INT_TYPE));
    }
}
