//! Python str/repr spelling for JSON values in method-not-found messages.
use serde_json::Value;
use std::fmt::Write;
use unicode_general_category::{get_general_category, GeneralCategory};

pub(super) fn python_float(value: f64) -> String {
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent: i32 = exponent.parse().unwrap();
    // Python switches at 1e-4 / 1e16 and always signs/pads the exponent.
    if !(-4..16).contains(&exponent) {
        format!("{mantissa}e{exponent:+03}")
    } else {
        let mut decimal = value.to_string();
        if !decimal.contains('.') {
            decimal.push_str(".0");
        }
        decimal
    }
}

pub(super) fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => text.clone(),
        Value::Number(number) if number.is_f64() => python_float(number.as_f64().unwrap()),
        Value::Number(number) => number.to_string(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!("{}: {}", quoted(key), python_repr(value)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::String(text) => quoted(text),
        _ => python_str(value),
    }
}

fn quoted(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::new();
    result.push(quote);
    for c in text.chars() {
        match c {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c == quote => {
                result.push('\\');
                result.push(c);
            }
            c if c != ' '
                && matches!(
                    get_general_category(c),
                    GeneralCategory::Control
                        | GeneralCategory::Format
                        | GeneralCategory::PrivateUse
                        | GeneralCategory::Surrogate
                        | GeneralCategory::Unassigned
                        | GeneralCategory::SpaceSeparator
                        | GeneralCategory::LineSeparator
                        | GeneralCategory::ParagraphSeparator
                ) =>
            {
                let n = c as u32;
                if n <= 0xff {
                    write!(result, "\\x{n:02x}").unwrap();
                } else if n <= 0xffff {
                    write!(result, "\\u{n:04x}").unwrap();
                } else {
                    write!(result, "\\U{n:08x}").unwrap();
                }
            }
            c => result.push(c),
        }
    }
    result.push(quote);
    result
}
