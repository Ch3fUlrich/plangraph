//! Find checkouts backing installed hooks, including targets outside discovery roots.
use crate::drift;
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};

fn checkout(path: &Path) -> Option<PathBuf> {
    let path = path.canonicalize().ok()?;
    path.ancestors()
        .find(|p| p.join(".git").exists() && drift::is_checkout(p))
        .map(Path::to_path_buf)
}

pub fn discover(
    repos: &mut Vec<PathBuf>,
    live: &[PathBuf],
    excludes: &[PathBuf],
) -> Result<(), ()> {
    let mut seen: HashSet<_> = repos.iter().filter_map(|p| p.canonicalize().ok()).collect();
    for path in live {
        if let Some(repo) = checkout(path) {
            if seen.insert(repo.clone()) {
                println!("UNIT {:?}", repo.display().to_string());
                repos.push(repo);
            }
        }
    }
    let mut index = 0;
    while index < repos.len() {
        let repo = repos[index].clone();
        index += 1;
        if repo.join(".repo-sync-pin").symlink_metadata().is_ok()
            || repo.canonicalize().is_ok_and(|p| excludes.contains(&p))
            || !drift::is_checkout(&repo)
        {
            continue;
        }
        let hooks = match drift::text(&repo, &["config", "--path", "--get", "core.hooksPath"]) {
            Ok(path) => repo.join(path),
            Err(Some(1)) => repo
                .join(drift::text(&repo, &["rev-parse", "--git-common-dir"]).map_err(|_| ())?)
                .join("hooks"),
            Err(_) => return Err(()),
        };
        let mut targets = vec![hooks.clone()];
        match fs::read_dir(&hooks) {
            Ok(entries) => {
                for entry in entries {
                    targets.push(entry.map_err(|_| ())?.path());
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) => {}
            Err(_) => return Err(()),
        }
        for target in targets {
            if let Some(hook_repo) = checkout(&target) {
                if seen.insert(hook_repo.clone()) {
                    println!(
                        "HOOK {:?} via {:?}",
                        hook_repo.display().to_string(),
                        target.display().to_string()
                    );
                    repos.push(hook_repo);
                }
            }
        }
    }
    Ok(())
}
