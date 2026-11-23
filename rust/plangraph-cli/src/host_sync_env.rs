//! Create systemd environment files without replacing operator configuration.
use super::Env;

#[path = "host_sync_quota.rs"]
mod quota;
#[path = "host_sync_shadow.rs"]
mod shadow;
use std::{
    ffi::OsString,
    fs,
    io::{self, Write},
    path::Path,
};

pub(super) fn exists(path: &Path) -> io::Result<bool> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => Ok(true),
        Ok(_) => Err(io::Error::other(format!(
            "{} is not a file",
            path.display()
        ))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

pub(super) fn create(path: &Path, contents: &[u8]) -> io::Result<()> {
    if exists(path)? {
        println!("SAME {}", path.display());
        return Ok(());
    }
    let parent = path.parent().expect("unit environment has a parent");
    pg_worker_core::run::record_io::ensure_private_dir(parent)?;
    let mut staged = tempfile::NamedTempFile::new_in(parent)?;
    staged.write_all(contents)?;
    staged.as_file().sync_all()?;
    match staged.persist_noclobber(path) {
        Ok(_) => println!("CREATED {}", path.display()),
        Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists && exists(path)? => {
            println!("SAME {}", path.display());
        }
        Err(error) => return Err(error.error),
    }
    Ok(())
}

fn assignment(key: &str, path: &Path) -> io::Result<String> {
    let value = path
        .to_str()
        .filter(|value| !value.contains(['\n', '\r', '\0']))
        .ok_or_else(|| io::Error::other(format!("invalid path for {key}")))?;
    let value = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('$', "\\$")
        .replace('`', "\\`");
    Ok(format!("{key}=\"{value}\"\n"))
}

fn install_throughput(home: &Path, env_file: &Path) -> io::Result<()> {
    if exists(env_file)? {
        println!("SAME {}", env_file.display());
        return Ok(());
    }
    let repo = home.join("code/plangraph");
    let origin = super::release::git(&repo, &["remote", "get-url", "origin"])
        .map_err(|_| io::Error::other(format!("cannot read origin for {}", repo.display())))?;
    let repo_id = crate::ci_watch::github::repository(&origin).map_err(io::Error::other)?;
    let mut contents = assignment("PLANGRAPH_THROUGHPUT_REPO", Path::new(&repo_id))?;
    contents.push_str("PLANGRAPH_THROUGHPUT_PROJECT=plangraph\n");
    contents.push_str(&assignment(
        "PLANGRAPH_THROUGHPUT_SNAPSHOT",
        &home.join(".local/share/plangraph/plan.snapshot.json"),
    )?);
    create(env_file, contents.as_bytes())
}

fn install(family: &str, home: &Path, args: &[OsString]) -> io::Result<()> {
    // The unit EnvironmentFile= directives use %h/.config.
    let config = home.join(".config/plangraph");
    match (family, args) {
        ("state-shadow", [plan_order, routing]) => shadow::install(
            home,
            &config.join("state-shadow"),
            [Path::new(plan_order), Path::new(routing)],
        ),
        ("state-shadow", _) => Err(io::Error::other(
            "state-shadow requires PLAN_ORDER_SETTINGS ROUTING_SETTINGS",
        )),
        ("disk-hygiene", []) => {
            let env_file = config.join("disk-hygiene-service.env");
            let legacy = config.join("disk-hygiene.env");
            for path in [&env_file, &legacy] {
                if exists(path)? {
                    println!("SAME {}", path.display());
                    return Ok(());
                }
            }
            let repo = home.join("code/plangraph");
            let values = [
                ("PG_REPO", repo.clone()),
                ("PG_WORKTREES", repo.join(".claude/worktrees")),
                ("PG_PINS", config.join("pins.json")),
            ];
            let mut content = String::new();
            for (key, path) in values {
                content.push_str(&assignment(key, &path)?);
            }
            create(&env_file, content.as_bytes())
        }
        ("throughput", []) => install_throughput(home, &config.join("throughput.env")),
        ("quota-alarms", []) => quota::install(home, &config.join("quota-alarms.env")),
        ("keeper-backup", []) => create(
            &config.join("keeper-backup.env"),
            include_bytes!("../../../deploy/systemd/keeper-backup.env.example"),
        ),
        _ => Err(io::Error::other(format!(
            "unknown unit family or unexpected arguments: {family}"
        ))),
    }
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let [family, rest @ ..] = args else {
        eprintln!("usage: host-sync --install-env FAMILY [PLAN_ORDER_SETTINGS ROUTING_SETTINGS]");
        return 2;
    };
    let result = (|| {
        let family = family
            .to_str()
            .ok_or_else(|| io::Error::other("invalid unit family"))?;
        let home = Env::from_process()
            .home
            .ok_or_else(|| io::Error::other("HOME is not set"))?;
        install(family, &home, rest)
    })();
    match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("FAIL unit environment: {error}");
            1
        }
    }
}
