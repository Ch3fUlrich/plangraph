//! Create systemd environment files without replacing operator configuration.
use super::Env;
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

fn install(family: &str, home: &Path) -> io::Result<()> {
    // The unit EnvironmentFile= directives use %h/.config.
    let config = home.join(".config/plangraph");
    match family {
        "disk-hygiene" => {
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
            create(&config.join("disk-hygiene.env"), content.as_bytes())
        }
        "keeper-backup" => create(
            &config.join("keeper-backup.env"),
            include_bytes!("../../../deploy/systemd/keeper-backup.env.example"),
        ),
        _ => Err(io::Error::other(format!("unknown unit family {family}"))),
    }
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let [family] = args else {
        eprintln!("usage: host-sync --install-env FAMILY");
        return 2;
    };
    let result = (|| {
        let family = family
            .to_str()
            .ok_or_else(|| io::Error::other("invalid unit family"))?;
        let home = Env::from_process()
            .home
            .ok_or_else(|| io::Error::other("HOME is not set"))?;
        install(family, &home)
    })();
    match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("FAIL unit environment: {error}");
            1
        }
    }
}
