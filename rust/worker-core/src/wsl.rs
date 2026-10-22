//! WSL defaults that affect attempt confinement.

fn strip_comment(line: &str) -> &str {
    let end = line.find(['#', ';']).unwrap_or(line.len());
    &line[..end]
}

fn has_disabled(text: &str, wanted: &str) -> bool {
    let mut section = String::new();

    for raw in text.lines() {
        let line = strip_comment(raw).trim();
        if line.is_empty() {
            continue;
        }

        if let Some(inner) = line.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
            section = inner.trim().to_ascii_lowercase();
            continue;
        }

        if section != wanted {
            continue;
        }

        if let Some((key, value)) = line.split_once('=') {
            if key.trim().eq_ignore_ascii_case("enabled")
                && value.trim().eq_ignore_ascii_case("false")
            {
                return true;
            }
        }
    }

    false
}

/// Reports WSL defaults that can widen the worker's host exposure.
///
/// `None` means `/etc/wsl.conf` is absent, so both findings are reported
/// conservatively.
pub fn wsl_findings(wsl_conf: Option<&str>) -> Vec<String> {
    let text = wsl_conf.unwrap_or("");
    let mut findings = Vec::new();

    if !has_disabled(text, "automount") {
        findings.push("automount enabled".to_string());
    }
    if !has_disabled(text, "interop") {
        findings.push("interop enabled".to_string());
    }

    findings
}

/// Whether an `osrelease` string identifies a WSL kernel.
pub fn is_wsl(osrelease: &str) -> bool {
    osrelease.to_ascii_lowercase().contains("microsoft")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expected(values: &[&str]) -> Vec<String> {
        values.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn absent_conf_reports_both() {
        assert_eq!(
            wsl_findings(None),
            expected(&["automount enabled", "interop enabled"])
        );
    }

    #[test]
    fn empty_conf_reports_both() {
        assert_eq!(
            wsl_findings(Some("")),
            expected(&["automount enabled", "interop enabled"])
        );
    }

    #[test]
    fn automount_disabled_only_reports_interop() {
        let conf = "[automount]\nenabled=false\n";
        assert_eq!(wsl_findings(Some(conf)), expected(&["interop enabled"]));
    }

    #[test]
    fn interop_disabled_only_reports_automount() {
        let conf = "[interop]\nenabled=false\n";
        assert_eq!(wsl_findings(Some(conf)), expected(&["automount enabled"]));
    }

    #[test]
    fn both_disabled_reports_nothing() {
        let conf = "[automount]\nenabled=false\n[interop]\nenabled=false\n";
        assert!(wsl_findings(Some(conf)).is_empty());
    }

    #[test]
    fn parse_ignores_case_space_and_comments() {
        let conf =
            "\n[ Automount ]\nEnabled = FALSE # no auto\n[interop]\n enabled=false ; no interop\n";
        assert!(wsl_findings(Some(conf)).is_empty());
    }

    #[test]
    fn commented_sections_do_not_count() {
        let conf = "# [automount]\n; enabled=false\n[interop]\n# enabled=false\n";
        assert_eq!(
            wsl_findings(Some(conf)),
            expected(&["automount enabled", "interop enabled"])
        );
    }

    #[test]
    fn is_wsl_matches_case_insensitively() {
        assert!(is_wsl("Linux 5.15.0-microsoft-standard-WSL2"));
        assert!(is_wsl("Linux 5.15.0-MICROSOFT-standard-WSL2"));
        assert!(!is_wsl("Linux 6.1.0-generic"));
    }
}
