//! Python dumps compatibility; exponent floats differ (1e-7 versus 1e-07).
use serde_json::{
    ser::{CompactFormatter, Formatter},
    Value,
};
use sha2::{Digest, Sha256};
use std::io::{self, Write};
struct PythonFormatter;
impl Formatter for PythonFormatter {
    fn begin_array_value<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_key<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_value<W: ?Sized + Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
    fn write_string_fragment<W: ?Sized + Write>(
        &mut self,
        w: &mut W,
        fragment: &str,
    ) -> io::Result<()> {
        for c in fragment.chars() {
            if c.is_ascii() {
                w.write_all(&[c as u8])?;
            } else {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    write!(w, "\\u{unit:04x}")?;
                }
            }
        }
        Ok(())
    }
}
fn encode(value: &Value, out: &mut Vec<u8>, f: &mut PythonFormatter) -> io::Result<()> {
    match value {
        Value::Object(m) => {
            f.begin_object(out)?;
            let mut keys: Vec<_> = m.keys().collect();
            keys.sort();
            for (i, key) in keys.into_iter().enumerate() {
                f.begin_object_key(out, i == 0)?;
                encode(&Value::String(key.clone()), out, f)?;
                f.begin_object_value(out)?;
                encode(&m[key], out, f)?;
            }
            f.end_object(out)
        }
        Value::Array(a) => {
            f.begin_array(out)?;
            for (i, v) in a.iter().enumerate() {
                f.begin_array_value(out, i == 0)?;
                encode(v, out, f)?;
            }
            f.end_array(out)
        }
        // Serialize scalar strings with serde_json's escape scanner and our formatter.
        Value::String(s) => {
            use serde::Serialize;
            s.serialize(&mut serde_json::Serializer::with_formatter(
                out,
                PythonFormatter,
            ))
            .map_err(io::Error::other)
        }
        _ => CompactFormatter.write_number_str(out, &value.to_string()),
    }
}
pub(crate) fn text(value: &Value) -> String {
    let mut out = Vec::new();
    encode(value, &mut out, &mut PythonFormatter).expect("vector write");
    String::from_utf8(out).expect("JSON is UTF-8")
}
pub(crate) fn card_hash(card: &Value) -> String {
    format!("{:x}", Sha256::digest(text(card).as_bytes()))
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn python_text_and_hash() {
        for (input, expected) in [
            (
                json!({"b":[1,2.5,null,true],"a":{"z":"xé😀","y":"q\"r\\s\n"},"estimate_tokens":7}),
                r#"{"a": {"y": "q\"r\\s\n", "z": "x\u00e9\ud83d\ude00"}, "b": [1, 2.5, null, true], "estimate_tokens": 7}"#,
            ),
            (
                json!({"stall_min":5,"note":"é","estimate_tokens":1000}),
                r#"{"estimate_tokens": 1000, "note": "\u00e9", "stall_min": 5}"#,
            ),
            (json!("\u{0}\u{8}\u{c}\r\t/\u{7f}"), r#""\u0000\b\f\r\t/""#),
        ] {
            assert_eq!(text(&input), expected);
            assert_eq!(
                card_hash(&input),
                format!("{:x}", Sha256::digest(expected.as_bytes()))
            );
            assert_eq!(card_hash(&input).len(), 64);
        }
    }
}
