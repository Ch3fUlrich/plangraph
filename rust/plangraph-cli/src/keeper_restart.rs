use std::path::Path;
use std::process::Command;

const SERVICE: &str = "plangraph-keeper.service";

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
    let unit = Command::new("systemctl")
        .args([
            "--user",
            "show",
            "--property=WorkingDirectory",
            "--value",
            SERVICE,
        ])
        .output()
        .map_err(|e| format!("read keeper unit: {e}"))?;
    let directory =
        String::from_utf8(unit.stdout).map_err(|_| "keeper unit directory is not UTF-8")?;
    let directory = Path::new(directory.trim());
    if !unit.status.success()
        || !directory.is_absolute()
        || directory.canonicalize().ok().as_ref() != Some(&checkout)
    {
        return Err(
            "the keeper unit's effective WorkingDirectory must name the candidate checkout".into(),
        );
    }

    let admin = || {
        let mut command = Command::new("python3");
        command
            .current_dir(&checkout)
            .env("PYTHONPATH", checkout.join("src"))
            .args(["-m", "plangraph.keeper.cli", "--config"])
            .arg(&config)
            .arg("admin");
        command
    };
    checked(
        admin().args(["migrate", "--dsn-file"]).arg(migration_dsn),
        "schema migration",
    )?;
    checked(admin().arg("check-schema"), "runtime schema verification")?;
    checked(
        Command::new("systemctl").args(["--user", "restart", SERVICE]),
        "service restart",
    )
}

fn checked(command: &mut Command, step: &str) -> Result<(), String> {
    let status = command.status().map_err(|e| format!("{step}: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{step}: {status}"))
    }
}
