//! Read the host into a `State`. All paths are resolved below `root` so tests
//! can use a temp dir; production passes "/".
use super::config::Config;
use super::download::sha256_file;
use super::plan::*;
use super::steps::{DEPS_MARKER, SEED_MARKER};
use std::path::{Path, PathBuf};

fn at(root: &Path, p: &Path) -> PathBuf {
    root.join(p.strip_prefix("/").unwrap_or(p))
}
fn read(root: &Path, p: &Path) -> Option<String> {
    std::fs::read_to_string(at(root, p)).ok()
}

pub fn observe(cfg: &Config, count: u32, root: &Path) -> Result<State, String> {
    let mut st = observe_fs(cfg, count, root)?;
    st.apt_missing = cfg
        .apt_packages
        .iter()
        .filter(|p| !dpkg_installed(p))
        .cloned()
        .collect();
    for u in &mut st.users {
        u.exists = user_exists(&u.name);
    }
    Ok(st)
}

fn user_exists(name: &str) -> bool {
    std::process::Command::new("id")
        .args(["-u", name])
        .output()
        .is_ok_and(|o| o.status.success())
}

fn dpkg_installed(pkg: &str) -> bool {
    std::process::Command::new("dpkg-query")
        .args(["-W", "-f=${Status}", pkg])
        .output()
        .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).contains("install ok installed"))
}

pub fn observe_fs(cfg: &Config, count: u32, root: &Path) -> Result<State, String> {
    let seed_dir = cfg.seed_dir();
    let mut st = State {
        sudoers: read(root, Path::new(SUDOERS_FILE)),
        seed_marker: read(root, &Path::new(STATE_DIR).join(SEED_MARKER)),
        playwright_deps: at(root, &Path::new(STATE_DIR).join(DEPS_MARKER)).exists(),
        ..Default::default()
    };
    if let Some(req) = &cfg.python_seed.requirements_file {
        st.requirements_sha = sha256_file(req).ok();
    }
    for name in [DL_RUNNER, DL_PYTHON] {
        let f = at(root, &cache_file(name));
        if f.exists() {
            st.cached.insert(name.into(), sha256_file(&f)?);
        }
    }
    for name in cfg.user_names(count) {
        let home = cfg.home(&name);
        let ms = at(root, &home.join(".cache/ms-playwright"));
        st.users.push(UserState {
            exists: false,
            lingering: at(root, &Path::new("/var/lib/systemd/linger").join(&name)).exists(),
            runner_version: read(root, &home.join("runner/.plangraph-version")),
            hosted_cache: std::fs::read_link(at(root, &home.join("hostedtoolcache/Python")))
                .is_ok_and(|t| t == seed_dir),
            playwright: std::fs::read_dir(ms).is_ok_and(|d| {
                d.flatten()
                    .any(|e| e.file_name().to_string_lossy().starts_with("chromium-"))
            }),
            temporal_link: std::fs::read_link(at(root, &home.join(".cache/temporal-cli")))
                .is_ok_and(|t| t == home.join("runner/.cache/temporal-cli")),
            run_one: read(root, &home.join("run-one.sh")),
            name,
        });
    }
    Ok(st)
}

#[cfg(test)]
mod tests {
    use super::super::config::sample;
    use super::super::template;
    use super::*;

    #[test]
    fn observes_files_below_the_root() {
        let cfg = sample();
        let d = tempfile::tempdir().unwrap();
        let w = |p: &str, c: &str| {
            let f = d.path().join(p);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, c).unwrap();
        };
        w("var/lib/systemd/linger/runner1", "");
        w(
            "home/runner1/run-one.sh",
            &template::run_one(&cfg.temporal_test_address),
        );
        w(
            "home/runner1/runner/.plangraph-version",
            &cfg.actions_runner.version,
        );
        w("var/lib/plangraph-runner/seed-marker", "m\n");
        w("var/cache/plangraph-runner/actions-runner.tar.gz", "abc");
        std::fs::create_dir_all(
            d.path()
                .join("home/runner1/.cache/ms-playwright/chromium-1"),
        )
        .unwrap();
        let st = observe_fs(&cfg, 2, d.path()).unwrap();
        assert!(st.users[0].lingering && st.users[0].playwright);
        assert!(st.users[0].run_one.is_some());
        assert_eq!(
            st.users[0].runner_version.as_deref(),
            Some(cfg.actions_runner.version.as_str())
        );
        assert!(!st.users[1].lingering && st.users[1].run_one.is_none());
        assert_eq!(st.seed_marker.as_deref(), Some("m\n"));
        assert_eq!(
            st.cached[DL_RUNNER],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
