//! Install the router watchdog configuration using the session that owns the stamp.
use pg_worker_core::run::record_io::atomic_write_private;
use plangraph_cli::atomic;
use std::{io, path::Path};

const TEMPLATE: &str = include_str!("../../../deploy/liveness/router.toml");

fn install(target: &str, config: &Path) -> io::Result<()> {
    if target.is_empty()
        || matches!(target, "." | "..")
        || !target
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid target name",
        ));
    }
    atomic::regular(config)?;
    let rendered = TEMPLATE.replace("<router-session>", target);
    atomic_write_private(config, rendered.as_bytes())
}

pub(super) fn run(args: &[String]) -> i32 {
    let [target, config] = args else {
        eprintln!("usage: plangraph install-router-liveness TARGET CONFIG");
        return 2;
    };
    match install(target, Path::new(config)) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("install-router-liveness: {error}");
            1
        }
    }
}
