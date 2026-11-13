mod drift;
use std::{env, path::PathBuf, process::ExitCode};

fn run() -> Result<bool, &'static str> {
    let mut repos = Vec::new();
    let mut policy = drift::Policy::default();
    let mut check_config = false;
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--repo") => repos.push(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--check-config") => check_config = true,
            Some("--host") => policy.host = args.next().ok_or("missing host")?.to_string_lossy().into(),
            Some("--allow-name") => policy.names.push(args.next().ok_or("missing name")?.to_string_lossy().into()),
            Some("--allow-email") => policy.emails.push(args.next().ok_or("missing email")?.to_string_lossy().into()),
            _ => return Err("usage: pg-repo-sync --repo PATH --check-config --host ID --allow-name NAME --allow-email EMAIL"),
        }
    }
    if !check_config
        || policy.host.is_empty()
        || policy.names.is_empty()
        || policy.emails.is_empty()
    {
        return Err("config check requires host and identity allow lists");
    }
    let mut alerts = false;
    for repo in repos {
        let differences = drift::check(&repo, &policy);
        if !differences.is_empty() {
            println!(
                "ALERT host={} path={:?} {}",
                policy.host.escape_default(),
                repo.display().to_string(),
                differences.join(" ")
            );
            alerts = true;
        }
    }
    Ok(alerts)
}
fn main() -> ExitCode {
    match run() {
        Ok(alerts) => u8::from(alerts).into(),
        Err(error) => {
            eprintln!("pg-repo-sync: {error}");
            ExitCode::FAILURE
        }
    }
}
