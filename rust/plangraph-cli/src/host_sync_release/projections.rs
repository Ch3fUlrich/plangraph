//! Refresh the generated routes with the checkout's existing registry generator.
use std::{
    env,
    io::{self, Write},
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

pub(super) fn refresh(repo: &Path, home: &Path) -> io::Result<()> {
    let config = env::var_os("XDG_CONFIG_HOME")
        .filter(|root| !root.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    let registry = config.join("plangraph/hosts.json");
    if !registry.try_exists()? {
        return Ok(());
    }
    let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
    command
        .current_dir(repo)
        .env("XDG_CONFIG_HOME", config)
        .env("PYTHONPATH", repo.join("src"))
        .args([
            "-P",
            "-m",
            "plangraph.fleet_sync",
            "genconfig",
            "write",
            "--registry",
        ])
        .arg(registry);
    let result = pg_worker_core::host_command::capture(&mut command, Duration::from_secs(30))?;
    if !result.status.success() {
        return Err(io::Error::other(
            "host registry projection generation failed",
        ));
    }
    io::stdout().lock().write_all(&result.stdout)
}
