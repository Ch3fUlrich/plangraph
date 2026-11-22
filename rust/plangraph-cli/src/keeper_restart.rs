use serde::de::DeserializeOwned;
use serde::Deserialize;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

const SERVICE: &str = "plangraph-keeper.service";
const BUS: &str = "org.freedesktop.systemd1";

pub fn run(args: &[String]) -> i32 {
    let [checkout, config, migration_dsn] = args else {
        eprintln!("usage: plangraph keeper-restart CHECKOUT KEEPER_CONFIG MIGRATION_DSN_FILE");
        return 2;
    };
    match restart(
        Path::new(checkout),
        Path::new(config),
        Path::new(migration_dsn),
    ) {
        Ok(()) => 0,
        Err(reason) => {
            eprintln!("keeper-restart: {reason}");
            1
        }
    }
}

fn restart(checkout: &Path, config: &Path, migration_dsn: &Path) -> Result<(), String> {
    let checkout = checkout
        .canonicalize()
        .map_err(|e| format!("checkout: {e}"))?;
    if !checkout.join("src/plangraph/keeper/cli.py").is_file() {
        return Err("checkout has no keeper admin CLI".into());
    }
    let config = std::path::absolute(config).map_err(|e| format!("keeper configuration: {e}"))?;
    let migration_dsn =
        std::path::absolute(migration_dsn).map_err(|e| format!("migration DSN path: {e}"))?;
    checked(
        Command::new("systemctl").args(["--user", "daemon-reload"]),
        "reload keeper unit",
    )?;
    let unit = KeeperUnit::read()?;
    if !unit.directory.is_absolute()
        || unit.directory.canonicalize().ok().as_ref() != Some(&checkout)
    {
        return Err(
            "the keeper unit's effective WorkingDirectory must name the candidate checkout".into(),
        );
    }
    if !unit.config.is_absolute()
        || unit
            .config
            .canonicalize()
            .ok()
            .zip(config.canonicalize().ok())
            .is_none_or(|(configured, supplied)| configured != supplied)
    {
        return Err("the supplied keeper configuration must match the unit's ExecStart".into());
    }

    // The user manager supplies its own environment and applies the unit's
    // files and unsets in systemd order. Keep inline values off the command line.
    let mut inline = tempfile::NamedTempFile::new()
        .map_err(|e| format!("create keeper environment file: {e}"))?;
    for assignment in &unit.environment {
        let (name, value) = assignment
            .split_once('=')
            .ok_or("invalid keeper Environment assignment")?;
        writeln!(inline, "{name}={}", quoted(value))
            .map_err(|e| format!("write keeper environment file: {e}"))?;
    }
    let admin = || {
        let mut command = Command::new("systemd-run");
        command
            .args(["--user", "--wait", "--pipe", "--collect", "--quiet"])
            .arg(format!("--working-directory={}", unit.directory.display()))
            .arg(format!(
                "--property=EnvironmentFile={}",
                inline.path().display()
            ));
        for (file, optional) in &unit.files {
            let prefix = if *optional { "-" } else { "" };
            command.arg(format!("--property=EnvironmentFile={prefix}{file}"));
        }
        if !unit.unsets.is_empty() {
            let unsets = unit
                .unsets
                .iter()
                .map(|entry| quoted(entry))
                .collect::<Vec<_>>()
                .join(" ");
            command.arg(format!("--property=UnsetEnvironment={unsets}"));
        }
        command
            .arg("--")
            .arg(&unit.python)
            .args(["-m", "plangraph.keeper.cli", "--config"])
            .arg(literal_arg(&unit.config)?)
            .arg("admin");
        Ok::<_, String>(command)
    };
    checked(
        admin()?
            .args(["migrate", "--dsn-file"])
            .arg(literal_arg(&migration_dsn)?),
        "schema migration",
    )?;
    checked(admin()?.arg("check-schema"), "runtime schema verification")?;
    checked(
        Command::new("systemctl").args(["--user", "restart", SERVICE]),
        "service restart",
    )
}

struct KeeperUnit {
    directory: PathBuf,
    python: String,
    config: PathBuf,
    environment: Vec<String>,
    files: Vec<(String, bool)>,
    unsets: Vec<String>,
}

impl KeeperUnit {
    fn read() -> Result<Self, String> {
        let paths: Vec<String> = property(&bus(&[
            "call",
            BUS,
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "LoadUnit",
            "s",
            SERVICE,
        ])?)?;
        let [path] = paths.as_slice() else {
            return Err("could not resolve the keeper unit object path".into());
        };
        let properties = bus(&[
            "get-property",
            BUS,
            path,
            "org.freedesktop.systemd1.Service",
            "WorkingDirectory",
            "ExecStartEx",
            "Environment",
            "EnvironmentFiles",
            "UnsetEnvironment",
        ])?;
        let [directory, start, environment, files, unsets] = properties
            .lines()
            .collect::<Vec<_>>()
            .as_slice()
            .try_into()
            .map_err(|_| "incomplete keeper unit properties")?;
        let start: Vec<serde_json::Value> = property(start)?;
        let [entry] = start.as_slice() else {
            return Err("keeper unit must have a single ExecStart".into());
        };
        let python = entry[0]
            .as_str()
            .filter(|python| Path::new(python).is_absolute())
            .ok_or("keeper ExecStart must name an absolute Python interpreter")?;
        let argv: Vec<String> = serde_json::from_value(entry[1].clone())
            .map_err(|_| "could not read keeper ExecStart arguments")?;
        let [argv0, module, daemon, config_flag, config] = argv.as_slice() else {
            return Err("keeper ExecStart must directly invoke the keeper daemon module".into());
        };
        if argv0 != python
            || module != "-m"
            || daemon != "plangraph.keeper.daemon"
            || config_flag != "--config"
            || config.contains('$')
            || entry[2].as_array().is_none_or(|flags| !flags.is_empty())
        {
            return Err("keeper ExecStart must directly invoke the keeper daemon module with a literal configuration path".into());
        }
        Ok(Self {
            directory: property(directory)?,
            python: python.into(),
            config: config.into(),
            environment: property(environment)?,
            files: property(files)?,
            unsets: property(unsets)?,
        })
    }
}

fn bus(args: &[&str]) -> Result<String, String> {
    let output = Command::new("busctl")
        .args(["--user", "--json=short"])
        .args(args)
        .output()
        .map_err(|e| format!("read keeper unit: {e}"))?;
    if !output.status.success() {
        return Err(format!("read keeper unit: {}", output.status));
    }
    String::from_utf8(output.stdout).map_err(|_| "keeper unit properties are not UTF-8".into())
}

fn property<T: DeserializeOwned>(line: &str) -> Result<T, String> {
    #[derive(Deserialize)]
    struct Property<T> {
        data: T,
    }
    serde_json::from_str::<Property<T>>(line)
        .map(|value| value.data)
        .map_err(|_| "invalid keeper unit property".into())
}

fn quoted(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

fn literal_arg(path: &Path) -> Result<String, String> {
    // systemd doubles a literal dollar in ExecStart arguments.
    path.to_str()
        .map(|path| path.replace('$', "$$"))
        .ok_or_else(|| "keeper path is not UTF-8".into())
}

fn checked(command: &mut Command, step: &str) -> Result<(), String> {
    let status = command.status().map_err(|e| format!("{step}: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{step}: {status}"))
    }
}
