//! Prepare the role environments and the Claude measurement hooks together.
use pg_worker_core::shadow_follow::register_measure_hook;
use serde_json::{json, Value};
use std::{fs, io, path::Path};

const ROLES: [&str; 2] = ["plan-order", "routing"];

fn prepare(bytes: &[u8]) -> io::Result<Option<Vec<u8>>> {
    let mut document: Value = serde_json::from_slice(bytes).map_err(io::Error::other)?;
    if !register_measure_hook(&mut document)? {
        return Ok(None);
    }
    let mut output = serde_json::to_vec_pretty(&document).map_err(io::Error::other)?;
    output.push(b'\n');
    Ok(Some(output))
}

/// Build `ROLE.json` from the operator's seed (`ROLE.seed.json`: principal, inbox, data_repo,
/// decision_base, github_repo, github_since and the optional lists) and the role's launch record
/// (live session id and working directory). An existing file is never replaced: the launcher
/// keeps its session and transcript current.
fn generate_config(home: &Path, config: &Path, role: &str) -> io::Result<()> {
    let target = config.join(format!("{role}.json"));
    if super::exists(&target)? {
        println!("SAME {}", target.display());
        return Ok(());
    }
    let seed = config.join(format!("{role}.seed.json"));
    let state = std::env::var_os("ROUTING_STATE_DIR")
        .filter(|dir| !dir.is_empty())
        .map_or_else(|| home.join(".local/state/routing"), Into::into);
    let record = state.join("launch").join(format!("{role}.json"));
    let (Ok(seed_bytes), Ok(record_bytes)) = (fs::read(&seed), fs::read(&record)) else {
        println!(
            "NEEDS {} and a launch record {} for {}",
            seed.display(),
            record.display(),
            target.display()
        );
        return Ok(());
    };
    let mut document: Value = serde_json::from_slice(&seed_bytes).map_err(io::Error::other)?;
    let record: Value = serde_json::from_slice(&record_bytes).map_err(io::Error::other)?;
    let object = document
        .as_object_mut()
        .ok_or_else(|| io::Error::other("expected a shadow seed object"))?;
    let (Some(session), Some(cwd)) = (record["session_id"].as_str(), record["cwd"].as_str()) else {
        println!(
            "NEEDS a session id and cwd in {} for {}",
            state.display(),
            target.display()
        );
        return Ok(());
    };
    for (key, value) in [
        ("role", json!(role)),
        (
            "directory",
            json!(home.join(".local/state/plangraph/state-shadow")),
        ),
        ("quota", json!(config.join("quota.json"))),
    ] {
        object.entry(key).or_insert(value);
    }
    object.insert("session_id".into(), json!(session));
    object.insert(
        "transcript".into(),
        json!(pg_worker_core::shadow_follow::transcript_for(
            home,
            session,
            Path::new(cwd)
        )),
    );
    let mut bytes = serde_json::to_vec_pretty(&document).map_err(io::Error::other)?;
    bytes.push(b'\n');
    super::create(&target, &bytes)
}

pub(super) fn install(home: &Path, config: &Path, profiles: [&Path; 2]) -> io::Result<()> {
    let hook = home.join(".local/bin/turn-budget-hook");
    if !super::super::is_executable(&hook) {
        return Err(io::Error::other(format!(
            "missing executable {}",
            hook.display()
        )));
    }
    pg_worker_core::run::record_io::ensure_private_dir(config)?;
    let _lock = pg_worker_core::lockfile::try_lock(&config.join("install.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another shadow install is running"))?;
    let mut changes = Vec::new();
    for profile in profiles {
        let profile = profile
            .canonicalize()
            .map_err(|err| io::Error::other(format!("{}: {err}", profile.display())))?;
        if let Some(bytes) = prepare(&fs::read(&profile)?)
            .map_err(|err| io::Error::other(format!("{}: {err}", profile.display())))?
        {
            changes.push((profile, bytes));
        }
    }

    for role in ROLES {
        let token = super::assignment("PG_TOKEN_FILE", &config.join(format!("{role}.token")))?;
        let source = super::assignment(
            "PLANGRAPH_STATE_CONFIG",
            &config.join(format!("{role}.json")),
        )?;
        super::create(
            &config.join(format!("{role}.env")),
            format!("{token}{source}").as_bytes(),
        )?;
        generate_config(home, config, role)?;
    }
    for (profile, bytes) in changes {
        pg_worker_core::run::record_io::atomic_write_private(&profile, &bytes)?;
        println!("REGISTERED {}", profile.display());
    }
    Ok(())
}
