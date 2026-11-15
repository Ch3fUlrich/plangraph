use regex::Regex;
use std::collections::BTreeMap;

pub const LOG_CAP: usize = 1024 * 1024;

pub fn scrub(text: &str) -> String {
    let mut text = text.to_owned();
    for pattern in [
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/]*@[^\s]+",
        r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16})",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        r"(?s)-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----|$)",
        r"(?i)(?:bearer\s+|authorization\s*[:=]\s*(?:basic|token)\s+)[^\s,;]+",
        r#"(?i)[A-Za-z0-9_]*(?:token|password|secret|api[_-]?key|access[_-]?key|[_-]key)[A-Za-z0-9_]*[\"']?\s*[=:]\s*[\"']?[^\s,;\"':][^\s,;\"']*"#,
        r"(?i)[?&](?:access_token|token|key|signature)=[^\s&#]+",
    ] {
        text = Regex::new(pattern)
            .unwrap()
            .replace_all(&text, "[REDACTED]")
            .into_owned();
    }
    text
}

pub fn failures(log: &str) -> BTreeMap<String, String> {
    let sanitized = scrub(log);
    let lines: Vec<_> = sanitized.lines().collect();
    let pytest = Regex::new(r"\bFAILED\s+([^\s]+::[^\s]+)").unwrap();
    let rust = Regex::new(r"\btest\s+(\S+)\s+\.\.\.\s+FAILED\b").unwrap();
    let panic = Regex::new(r"thread '([^']+)' panicked").unwrap();
    let mut found = BTreeMap::new();
    for (i, line) in lines.iter().enumerate() {
        for re in [&pytest, &rust, &panic] {
            if let Some(capture) = re.captures(line) {
                let id = capture[1].to_owned();
                if id == "main" {
                    continue;
                }
                let start = i.saturating_sub(19);
                let end = (start + 40).min(lines.len());
                found
                    .entry(id)
                    .or_insert_with(|| scrub(&lines[start..end].join("\n")));
            }
        }
    }
    if found.is_empty() {
        let start = lines.len().saturating_sub(40);
        found.insert("<job-failure>".into(), scrub(&lines[start..].join("\n")));
    }
    found
}
