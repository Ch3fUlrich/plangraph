//! Portable execution group contract.
use std::{
    ffi::{OsStr, OsString},
    io,
    path::Path,
    process::Child,
    thread,
    time::{Duration, Instant},
};
pub const RUNS_SLICE: &str = "agent-runs.slice";
#[derive(Debug, Clone)]
pub enum Confinement {
    Scope { slice: String },
    Plain,
}
impl Default for Confinement {
    fn default() -> Self {
        Self::Scope {
            slice: RUNS_SLICE.into(),
        }
    }
}
#[derive(Debug, Default)]
pub struct RunLimits {
    pub memory_max: Option<String>,
    pub tasks_max: Option<u32>,
}
pub struct LaunchSpec<'a> {
    pub program: &'a OsStr,
    pub args: &'a [OsString],
    pub cwd: &'a Path,
    pub env: &'a [(OsString, OsString)],
    pub stdin_piped: bool,
}
#[derive(Debug)]
pub struct Identity {
    pub pid: u32,
    pub start_time: Option<u64>,
    pub group: Option<String>,
}
pub trait RunGroup: Send + Sync {
    fn identity(&self) -> &Identity;
    fn members(&self) -> io::Result<Vec<u32>>;
    fn terminate(&self);
    fn kill(&self);
}
pub struct Grouped {
    pub child: Child,
    pub group: Box<dyn RunGroup>,
}
#[derive(Debug, thiserror::Error)]
pub enum LaunchError {
    #[error("invalid unit")]
    BadUnit,
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error("scope unavailable: {0}")]
    ScopeUnavailable(String),
    #[error("scope not entered")]
    ScopeNotEntered,
    #[error("unsupported")]
    Unsupported,
    #[error("job exists")]
    JobExists,
    #[error("job unavailable")]
    JobUnavailable,
}
pub trait Launcher: Send + Sync {
    fn launch(
        &self,
        unit: &str,
        spec: &LaunchSpec<'_>,
        confinement: &Confinement,
        limits: &RunLimits,
    ) -> Result<Grouped, LaunchError>;
}
pub struct SystemLauncher;
impl Launcher for SystemLauncher {
    fn launch(
        &self,
        unit: &str,
        spec: &LaunchSpec<'_>,
        confinement: &Confinement,
        limits: &RunLimits,
    ) -> Result<Grouped, LaunchError> {
        #[cfg(target_os = "linux")]
        {
            super::group_unix::launch(unit, spec, confinement, limits, &Default::default())
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (unit, spec, confinement, limits);
            Err(LaunchError::Unsupported)
        }
    }
}
pub fn validate_unit(unit: &str) -> Result<(), LaunchError> {
    if (1..=100).contains(&unit.len())
        && unit
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        Ok(())
    } else {
        Err(LaunchError::BadUnit)
    }
}
pub fn unit_name(run_id: &str) -> String {
    format!("pg-{run_id}")
}
pub fn job_name(unit: &str) -> String {
    format!("Local\\{unit}")
}
#[derive(Debug, PartialEq, Eq)]
pub enum StopOutcome {
    Empty,
    Survivors(Vec<u32>),
}
pub fn stop_group(group: &dyn RunGroup, grace: Duration, kill_timeout: Duration) -> StopOutcome {
    let mut last = vec![group.identity().pid];
    let mut check = || match group.members() {
        Ok(p) => {
            let empty = p.is_empty();
            last = p;
            empty
        }
        Err(_) => false,
    };
    if check() {
        return StopOutcome::Empty;
    }
    for (duration, polite) in [(grace, true), (kill_timeout, false)] {
        if polite && duration.is_zero() {
            continue;
        }
        if polite {
            group.terminate();
        } else {
            group.kill();
        }
        let deadline = Instant::now() + duration;
        let mut repeat = Instant::now() + Duration::from_secs(2);
        loop {
            if check() {
                return StopOutcome::Empty;
            }
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            if !polite && now >= repeat {
                group.kill();
                repeat = now + Duration::from_secs(2);
            }
            thread::sleep(Duration::from_millis(25).min(deadline - now));
        }
    }
    StopOutcome::Survivors(last)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn names() {
        for s in ["a", "AZaz09_-", &"a".repeat(100)] {
            assert!(validate_unit(s).is_ok());
        }
        for s in ["", "a.b", "a/b", "é", &"a".repeat(101)] {
            assert!(matches!(validate_unit(s), Err(LaunchError::BadUnit)));
        }
        assert_eq!(unit_name("run"), "pg-run");
        assert_eq!(job_name("pg-run"), "Local\\pg-run");
    }
}
