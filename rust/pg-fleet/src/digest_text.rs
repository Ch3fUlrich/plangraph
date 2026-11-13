use crate::handoff::re;
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};

pub fn to_dt(s: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(s)
        .map(|t| t.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            NaiveDateTime::parse_from_str(s.trim_end_matches('Z'), "%Y-%m-%dT%H:%M")
                .ok()
                .map(|t| t.and_utc())
        })
        .or_else(|| {
            NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|t| t.and_utc())
        })
        .or_else(|| {
            NaiveDate::parse_from_str(s, "%Y-%m-%d")
                .ok()
                .and_then(|d| d.and_hms_opt(0, 0, 0))
                .map(|t| t.and_utc())
        })
}
pub fn age(sec: i64) -> String {
    let m = sec.max(0) / 60;
    if m < 60 {
        format!("{m}m")
    } else if m < 48 * 60 {
        format!("{}h{:02}m", m / 60, m % 60)
    } else {
        format!("{}d", m / 1440)
    }
}
pub fn stamp(t: DateTime<Utc>) -> String {
    t.format("%Y-%m-%dT%H:%MZ").to_string()
}
pub fn role(name: &str) -> String {
    let r = crate::fleet_name::role_of(name);
    if r == "routing-00" {
        "routing".into()
    } else {
        r
    }
}
pub fn safe(s: &str) -> bool {
    re(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$").is_match(s)
}
pub fn plain(s: &str) -> String {
    let s = re(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
        .replace_all(s, "");
    re(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\x{2028}\x{2029}]")
        .replace_all(&s, " ")
        .into_owned()
}
pub fn cut(s: &str, n: usize) -> String {
    let s = plain(s);
    let indent = s.len() - s.trim_start_matches(' ').len();
    let s = " ".repeat(indent) + &s.split_whitespace().collect::<Vec<_>>().join(" ");
    truncate(&s, n)
}
pub fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() > n {
        s.chars().take(n - 1).collect::<String>() + "…"
    } else {
        s.to_owned()
    }
}
pub fn scrub(s: &str) -> String {
    let mut s = s.to_owned();
    for pat in [
        r"\bsk[-_][A-Za-z0-9_-]{16,}",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bxox[bp]-[A-Za-z0-9-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        r"(?i)\b[\w-]*(?:key|token|secret|passw(?:or)?d|dsn|credential)[\w-]*\s*=\s*\S{6,}",
        r"://[^/\s:@]+:[^/\s@]+@",
    ] {
        s = re(pat).replace_all(&s, "[redacted]").into_owned();
    }
    s
}
pub fn fit(mut lines: Vec<String>, n: usize) -> Vec<String> {
    if lines.len() > n {
        let count = lines.len() - n + 1;
        lines.truncate(n - 1);
        lines.push(format!("... {count} more lines cut"));
    }
    lines
}
pub fn mtime(p: &std::path::Path) -> Option<DateTime<Utc>> {
    p.metadata().ok()?.modified().ok().map(Into::into)
}
pub fn same_dir(a: &str, b: &str) -> bool {
    !a.is_empty()
        && !b.is_empty()
        && crate::paths::resolve(std::path::Path::new(a)).ok()
            == crate::paths::resolve(std::path::Path::new(b)).ok()
}
pub fn usable(p: &std::path::Path) -> bool {
    p.is_file() && !crate::paths::secret(p)
}
pub fn glob(dir: &std::path::Path, suffix: &str) -> Vec<std::path::PathBuf> {
    let mut out: Vec<_> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .is_some_and(|n| n.to_string_lossy().ends_with(suffix))
        })
        .collect();
    out.sort();
    out
}
