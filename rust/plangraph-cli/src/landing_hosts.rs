use crate::feeder::Env;
use pg_worker_core::hosts::{HostEntry, HostRegistry};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Serialize, Deserialize)]
struct Request {
    root: PathBuf,
    stamp: PathBuf,
    head: String,
    sync: Option<PathBuf>,
}

#[derive(Deserialize)]
struct BuildStamp {
    head: String,
    binaries: Vec<Binary>,
}

#[derive(Deserialize)]
struct Binary {
    path: PathBuf,
    sha256: String,
}

fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let result = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(|_| "cannot run git")?;
    result
        .status
        .success()
        .then_some(result.stdout)
        .ok_or_else(|| format!("git {} failed", args[0]))
}

fn text(root: &Path, args: &[&str]) -> Result<String, String> {
    String::from_utf8(git(root, args)?)
        .map(|s| s.trim_end().into())
        .map_err(|_| "invalid git output".into())
}

fn valid_head(head: &str) -> bool {
    head.len() == 40
        && head
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn hash_file(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path).map_err(|_| "installed binary is unavailable")?;
    let mut digest = Sha256::new();
    let mut chunk = [0; 32 * 1024];
    loop {
        let n = file
            .read(&mut chunk)
            .map_err(|_| "cannot read installed binary")?;
        if n == 0 {
            return Ok(format!("{:x}", digest.finalize()));
        }
        digest.update(&chunk[..n]);
    }
}

fn verify(request: &Request) -> Result<(), String> {
    let root = &request.root;
    let head = &request.head;
    if !valid_head(head)
        || text(root, &["symbolic-ref", "--quiet", "HEAD"])? != "refs/heads/main"
        || text(root, &["rev-parse", "HEAD"])? != *head
        || !text(root, &["status", "--porcelain=v1", "--untracked-files=no"])?.is_empty()
    {
        return Err("main checkout differs from the merged head".into());
    }
    let hooks = PathBuf::from(text(root, &["rev-parse", "--git-path", "hooks"])?);
    let hooks = if hooks.is_absolute() {
        hooks
    } else {
        root.join(hooks)
    };
    let scripts = text(
        root,
        &["ls-tree", "-r", "--name-only", head, "--", "tools/hooks/"],
    )?;
    if !scripts.lines().any(|path| path == "tools/hooks/pre-push") {
        return Err("merged tree has no pre-push hook".into());
    }
    for source in scripts.lines() {
        let name = source
            .strip_prefix("tools/hooks/")
            .ok_or("invalid hook path")?;
        let deployed =
            fs::read(hooks.join(name)).map_err(|_| format!("hook {name} is unavailable"))?;
        if deployed != git(root, &["show", &format!("{head}:{source}")])? {
            return Err(format!("hook {name} differs from the merged tree"));
        }
    }
    let data = fs::read(&request.stamp).map_err(|_| "build stamp is unavailable")?;
    let stamp: BuildStamp = serde_json::from_slice(&data).map_err(|_| "invalid build stamp")?;
    if stamp.head != *head || stamp.binaries.is_empty() {
        return Err("build stamp differs from the merged head or lists no binaries".into());
    }
    for binary in stamp.binaries {
        if !binary.path.is_absolute() || hash_file(&binary.path)? != binary.sha256 {
            return Err("a stamped installed binary differs".into());
        }
    }
    Ok(())
}

pub(super) fn agent() -> Result<Value, String> {
    let request: Request = serde_json::from_reader(io::stdin().lock())
        .map_err(|_| "invalid host verification request")?;
    if !request.root.is_absolute() || !request.stamp.is_absolute() || !valid_head(&request.head) {
        return Err("invalid host checkout, stamp or merged head".into());
    }
    if let Some(executable) = &request.sync {
        if !executable.is_absolute() {
            return Err("landing sync executable must be absolute".into());
        }
        let result = Command::new(executable)
            .arg(&request.root)
            .arg(&request.head)
            .output()
            .map_err(|_| "cannot run host sync")?;
        if !result.status.success() {
            return Err("host sync failed".into());
        }
    }
    match verify(&request) {
        Ok(()) => Ok(json!({"verified": true, "head": request.head,
                           "checkout": request.head, "hooks": request.head, "build": request.head})),
        Err(reason) => Ok(json!({"verified": false, "reason": reason})),
    }
}

fn request(host: &HostEntry, local: bool, body: &Request) -> Result<Value, String> {
    let mut command = if local {
        let mut command =
            Command::new(std::env::current_exe().map_err(|_| "current executable unavailable")?);
        command.args(["landing", "host-agent"]);
        command
    } else {
        let route = host.ssh_route().ok_or("host has no SSH route")?;
        let mut command = Command::new("ssh");
        command.args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "--",
            &route,
            "plangraph",
            "landing",
            "host-agent",
        ]);
        command
    };
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|_| "cannot start landing host agent")?;
    let written = child
        .stdin
        .take()
        .ok_or("no host agent stdin")?
        .write_all(&serde_json::to_vec(body).map_err(|_| "cannot encode host request")?);
    let output = child
        .wait_with_output()
        .map_err(|_| "cannot wait for host agent")?;
    if written.is_err() || !output.status.success() {
        return Err("landing host agent failed".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "invalid host verification receipt".into())
}

fn converge(
    registry: &HostRegistry,
    repo: &str,
    head: &str,
    mut invoke: impl FnMut(&HostEntry, bool, &Request) -> Result<Value, String>,
) -> Result<Value, String> {
    let mut verified = Vec::new();
    let mut errors = Vec::new();
    for host in &registry.hosts {
        let Some(checkout) = host.repos.values().find(|entry| {
            let remote = entry.remote.trim_end_matches(".git");
            remote
                .rsplit_once(':')
                .or_else(|| remote.split_once("://"))
                .is_some_and(|(_, path)| path.eq_ignore_ascii_case(repo))
                || remote.ends_with(&format!("/{repo}"))
        }) else {
            continue;
        };
        let result = (|| {
            let stamp = host
                .env
                .get("PG_LANDING_BUILD_STAMP")
                .ok_or("build stamp is unspecified")?;
            let mut body = Request {
                root: checkout.root.clone().into(),
                stamp: stamp.into(),
                head: head.into(),
                sync: None,
            };
            let local = registry.self_host == host.id;
            let first = invoke(host, local, &body)?;
            let ok = |receipt: &Value| {
                receipt["verified"] == true
                    && ["head", "checkout", "hooks", "build"]
                        .iter()
                        .all(|field| receipt[*field] == head)
            };
            if ok(&first) {
                return Ok(false);
            }
            let reason = first["reason"]
                .as_str()
                .unwrap_or("verification receipt differs");
            eprintln!("landing host {} lagging: {reason}", host.id);
            body.sync = Some(
                host.env
                    .get("PG_LANDING_SYNC_EXECUTABLE")
                    .ok_or("sync executable is unspecified")?
                    .into(),
            );
            let next = invoke(host, local, &body)?;
            ok(&next).then_some(true).ok_or_else(|| {
                next["reason"]
                    .as_str()
                    .unwrap_or("host is still lagging")
                    .to_owned()
            })
        })();
        match result {
            Ok(synced) => verified.push(json!({"host": host.id, "head": head, "synced": synced,
                                               "checkout": head, "hooks": head, "build": head})),
            Err(reason) => errors.push(format!("{}: {reason}", host.id)),
        }
    }
    if !errors.is_empty() {
        return Err(errors.join(", "));
    }
    if verified.len() != 3 {
        return Err(format!(
            "landing roster has {} verified repository hosts, expected three",
            verified.len()
        ));
    }
    Ok(json!({"verified": verified, "count": verified.len()}))
}

pub(super) fn run(env: &Env, args: &[String]) -> Result<Value, String> {
    let [repo, head] = args else {
        return Err("usage: plangraph landing verify-hosts OWNER/REPO MERGED_SHA".into());
    };
    if !valid_head(head) {
        return Err("landing verification requires an exact merged head".into());
    }
    let path = env
        .get("PG_LANDING_HOSTS")
        .ok_or("PG_LANDING_HOSTS required")?;
    let registry =
        HostRegistry::load(Path::new(path)).map_err(|_| "cannot load landing host registry")?;
    converge(&registry, repo, head, request)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lagging_roster_host_is_synced_and_three_receipts_are_returned() {
        let mut registry = HostRegistry::from_json_str(include_str!(
            "../../../docs/schemas/examples/hosts.example.json"
        ))
        .unwrap();
        let checkout = registry.hosts[0].repos.clone();
        let lagging = registry.hosts[1].id.clone();
        for host in &mut registry.hosts {
            host.repos = checkout.clone();
            host.env
                .insert("PG_LANDING_BUILD_STAMP".into(), "fixture-stamp".into());
            host.env
                .insert("PG_LANDING_SYNC_EXECUTABLE".into(), "fixture-sync".into());
        }
        let head = "a".repeat(40);
        let mut syncs = Vec::new();
        let receipt = converge(&registry, "fleet/plangraph", &head, |host, _, req| {
            if host.id == lagging && req.sync.is_none() {
                return Ok(json!({"verified": false, "reason": "stale hooks"}));
            }
            if req.sync.is_some() {
                syncs.push(host.id.clone());
            }
            Ok(json!({"verified": true, "head": head, "checkout": head, "hooks":head, "build":head}))
        }).unwrap();
        assert_eq!(syncs, [lagging]);
        assert_eq!(receipt["count"], 3);
        assert!(receipt["verified"].as_array().unwrap().iter().all(|host| {
            host["checkout"] == head && host["hooks"] == head && host["build"] == head
        }));
    }
}
