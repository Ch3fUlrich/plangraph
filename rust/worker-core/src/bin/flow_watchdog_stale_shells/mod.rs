use super::Alert;
use std::{fs, os::unix::fs::MetadataExt, path::Path};

const MIN_AGE_SECONDS: f64 = 7200.0;

pub(super) fn alerts(proc: &Path) -> Vec<Alert> {
    match scan(proc) {
        Some(alerts) => alerts,
        None => vec!["deleted-cwd-shell unknown=proc".to_owned().into()],
    }
}

fn scan(proc: &Path) -> Option<Vec<Alert>> {
    let uptime: f64 = fs::read_to_string(proc.join("uptime"))
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()?;
    // SAFETY: sysconf takes a constant selector and returns the clock frequency.
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if hz <= 0 || !uptime.is_finite() || uptime < 0.0 {
        return None;
    }
    let mut alerts: Vec<_> = fs::read_dir(proc)
        .ok()?
        .filter_map(Result::ok)
        .filter_map(|entry| inspect(&entry.path(), uptime, hz as f64))
        .collect();
    alerts.sort_by(|a, b| a.active.cmp(&b.active));
    Some(alerts)
}

fn inspect(entry: &Path, uptime: f64, hz: f64) -> Option<Alert> {
    let pid: u32 = entry.file_name()?.to_str()?.parse().ok()?;
    let stat_path = entry.join("stat");
    let stat = fs::read_to_string(&stat_path).ok()?;
    let (shell, start) = shell_identity(&stat)?;
    let age = uptime - start as f64 / hz;
    // A cwd can have a literal " (deleted)" suffix and still be linked.
    if age <= MIN_AGE_SECONDS || fs::metadata(entry.join("cwd")).ok()?.nlink() != 0 {
        return None;
    }
    if shell_identity(&fs::read_to_string(stat_path).ok()?)? != (shell, start) {
        return None;
    }
    let active = format!("deleted-cwd-shell pid={pid} start_ticks={start}");
    Some(Alert {
        active,
        message: format!(
            "deleted-cwd-shell pid={pid} shell={shell} age_seconds={}",
            age.ceil()
        ),
        recipient: None,
    })
}

fn shell_identity(stat: &str) -> Option<(&str, u64)> {
    let (command, fields) = stat.rsplit_once(')')?;
    let (_, shell) = command.split_once('(')?;
    if !matches!(
        shell,
        "bash" | "sh" | "dash" | "zsh" | "ksh" | "ash" | "fish"
    ) {
        return None;
    }
    let mut fields = fields.split_whitespace();
    if matches!(fields.next()?, "Z" | "X" | "x") {
        return None;
    }
    Some((shell, fields.nth(18)?.parse().ok()?))
}
