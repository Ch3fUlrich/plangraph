//! Validated local-only admission configuration.
use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};

#[derive(Default)]
pub(crate) struct Options {
    host: Option<String>,
    placement: Option<String>,
    max_runs: Option<usize>,
    max_queued: Option<usize>,
    pub no_scope: bool,
}
pub(crate) fn valid_host(s: &str) -> bool {
    (1..=32).contains(&s.len())
        && s.as_bytes()[0].is_ascii_lowercase()
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}
impl Options {
    pub fn set(&mut self, flag: &str, value: &str) -> Result<(), String> {
        match flag {
            "--host-id" => self.host = Some(value.into()),
            "--placement" => self.placement = Some(value.into()),
            "--max-runs" | "--max-queued" => {
                let n = value
                    .parse::<usize>()
                    .map_err(|_| format!("invalid {flag}"))?;
                if flag == "--max-runs" {
                    if !(1..=64).contains(&n) {
                        return Err("invalid --max-runs".into());
                    }
                    self.max_runs = Some(n);
                } else {
                    if n > 1024 {
                        return Err("invalid --max-queued".into());
                    }
                    self.max_queued = Some(n);
                }
            }
            _ => unreachable!(),
        }
        Ok(())
    }
    pub fn resolve(self) -> Result<SupervisorCfg, String> {
        self.resolve_with(|key| std::env::var(key).ok())
    }
    fn resolve_with(self, env: impl Fn(&str) -> Option<String>) -> Result<SupervisorCfg, String> {
        let host = self
            .host
            .or_else(|| env("PLANGRAPH_HOST"))
            .unwrap_or("local".into());
        if !valid_host(&host) {
            return Err("invalid --host-id or PLANGRAPH_HOST".into());
        }
        let placement = self
            .placement
            .or_else(|| env("PLANGRAPH_PLACEMENT"))
            .unwrap_or("off".into());
        if placement != "off" {
            return Err("placement mode not implemented: off only".into());
        }
        let mut cfg = SupervisorCfg::new(Config {
            host,
            ..Config::default()
        });
        cfg.run.limits.windows_holder_max_s =
            Some(pg_worker_core::run::job_holder_windows::validate_max_s(
                env("PG_SPAWND_WINDOWS_HOLDER_MAX_S").as_deref(),
            )?);
        if self.no_scope {
            cfg.run.confinement = Confinement::Plain;
        }
        if let Some(n) = self.max_runs {
            cfg.max_runs = n;
        }
        if let Some(n) = self.max_queued {
            cfg.max_queued = n;
        }
        Ok(cfg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_holder_daemon_setting() {
        for (value, valid) in [
            ("600", true),
            ("172800", true),
            ("599", false),
            ("172801", false),
            ("NaN", false),
            ("inf", false),
        ] {
            let cfg = Options::default().resolve_with(|key| {
                (key == "PG_SPAWND_WINDOWS_HOLDER_MAX_S").then(|| value.into())
            });
            assert_eq!(cfg.is_ok(), valid);
            if let Ok(cfg) = cfg {
                assert_eq!(cfg.run.limits.windows_holder_max_s, value.parse().ok());
            }
        }
        assert_eq!(
            Options::default()
                .resolve_with(|_| None)
                .unwrap()
                .run
                .limits
                .windows_holder_max_s,
            Some(28800)
        );
    }
    #[test]
    fn flags_table() {
        for (flag, value, valid) in [
            ("--host-id", "local-1", true),
            ("--host-id", "Bad", false),
            ("--host-id", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", true),
            ("--host-id", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", false),
            ("--host-id", "loCal", false),
            ("--host-id", "a_b", false),
            ("--host-id", "", false),
            ("--max-runs", "1", true),
            ("--max-runs", "0", false),
            ("--max-runs", "65", false),
            ("--max-runs", "abc", false),
            ("--max-queued", "0", true),
            ("--max-queued", "1025", false),
            ("--placement", "off", true),
            ("--placement", "shadow", false),
            ("--placement", "on", false),
            ("--placement", "bogus", false),
        ] {
            let mut options = Options::default();
            let result = options
                .set(flag, value)
                .and_then(|()| options.resolve_with(|_| None));
            assert_eq!(result.is_ok(), valid, "{flag} {value}");
        }
        for (key, value) in [("PLANGRAPH_HOST", "Bad"), ("PLANGRAPH_PLACEMENT", "on")] {
            assert!(Options::default()
                .resolve_with(|k| (k == key).then(|| value.into()))
                .is_err());
        }
        let cfg = Options::default().resolve_with(|_| None).unwrap();
        assert_eq!(cfg.run.host, "local");
        assert_eq!((cfg.max_runs, cfg.max_queued), (3, 64));
        assert!(matches!(cfg.run.confinement, Confinement::Scope { .. }));
    }

    #[test]
    fn resolved_limits_and_plain_confinement() {
        let mut options = Options {
            no_scope: true,
            ..Options::default()
        };
        options.set("--max-runs", "5").unwrap();
        options.set("--max-queued", "0").unwrap();
        let cfg = options.resolve_with(|_| None).unwrap();
        assert_eq!(cfg.max_runs, 5);
        assert_eq!(cfg.max_queued, 0);
        assert!(matches!(cfg.run.confinement, Confinement::Plain));
    }
}
