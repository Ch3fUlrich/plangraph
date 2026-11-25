//! The `active` line of a host alert: `host-disk: host="H"` (an aggregate that names no filesystem),
//! `host-disk: host="H" fs="D"` and `host-disk: host="H" fs="D" measured` (one filesystem), and
//! `host-code: host="H"`. Every part is a JSON string, so a key is one line.

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Key {
    pub disk: bool,
    pub host: String,
    pub fs: Option<String>,
    pub measured: bool,
}

fn json_prefix(text: &str) -> Option<(String, &str)> {
    let mut values = serde_json::Deserializer::from_str(text).into_iter::<String>();
    let value = values.next()?.ok()?;
    Some((value, &text[values.byte_offset()..]))
}

impl Key {
    pub fn disk(host: &str, fs: Option<&str>, measured: bool) -> Self {
        Self {
            disk: true,
            host: host.to_owned(),
            fs: fs.map(str::to_owned),
            measured,
        }
    }

    pub fn code(host: &str) -> Self {
        Self {
            disk: false,
            host: host.to_owned(),
            fs: None,
            measured: false,
        }
    }

    pub fn parse(line: &str) -> Option<Self> {
        let (disk, rest) = match line.strip_prefix("host-disk: host=") {
            Some(rest) => (true, rest),
            None => (false, line.strip_prefix("host-code: host=")?),
        };
        let (host, tail) = json_prefix(rest)?;
        let (fs, tail) = match tail.strip_prefix(" fs=") {
            Some(tail) => json_prefix(tail).map(|(fs, tail)| (Some(fs), tail))?,
            None => (None, tail),
        };
        let measured = match tail {
            "" => false,
            " measured" if disk => true,
            _ => return None,
        };
        Some(Self {
            disk,
            host,
            fs,
            measured,
        })
    }

    pub fn render(&self) -> String {
        let json = |text: &str| serde_json::to_string(text).unwrap();
        let mut line = format!(
            "{}: host={}",
            if self.disk { "host-disk" } else { "host-code" },
            json(&self.host)
        );
        if let Some(fs) = &self.fs {
            line.push_str(&format!(" fs={}", json(fs)));
        }
        if self.measured {
            line.push_str(" measured");
        }
        line
    }

    /// The crossing a key belongs to: the bare and the measured key of a filesystem are one crossing.
    pub fn crossing(&self) -> Self {
        Self {
            measured: false,
            ..self.clone()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_round_trip_and_reject_foreign_text() {
        for key in [
            Key::disk("a\n\"b", None, false),
            Key::disk("h", Some("64768"), false),
            Key::disk("h", Some("64768"), true),
            Key::code("h"),
        ] {
            let line = key.render();
            assert_eq!(line.lines().count(), 1, "{line}");
            assert_eq!(Key::parse(&line), Some(key));
        }
        for line in [
            "host-disk: host=h",
            "host-disk: host=\"h\" extra",
            "busy-quiet x",
        ] {
            assert_eq!(Key::parse(line), None, "{line}");
        }
    }
}
