//! Which deploy families a landed range ships, attributed across the whole train
//! (TRAIN-DEPLOY-DERIVE-LANDED-PATH-ATTRIBUTION, TRAIN-DEPLOYS-ITS-FAMILIES).
//!
//! Every path is classified by its net status between the train base and the train head (added, modified,
//! removed at the head), never per topic head: a unit a merge resolution adds ships, and so does a binary a
//! stacked part edits after an earlier part of the same train added it. Each commit of the range that
//! touches a shipped path (a merge commit through its resolution diff) attributes the family to its
//! `Plan-Item` keys and `Deploy:` hosts.
//!
//! With `deploy/manifest.toml` in the head tree a path maps to its manifest family: a unit file to its
//! `[[unit]]` entry, any file of a crate to every `[[binary]]` of that crate and of the crates that depend on
//! it by path. Without a manifest the keeper's rule applies: a unit file under `deploy/systemd`, an added
//! binary entry point.
use super::git;
use crate::deploy_manifest::{is_template, BinaryEntry, Manifest, Scope, UnitEntry, MANIFEST_PATH};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// One family the range ships.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Family {
    /// The manifest entry name (`pg-feeder`), or `bin:<name>`; the unit file name without a manifest.
    pub name: String,
    /// What is installed and checked: the enabled unit and the service its timer starts, or `bin:<name>`.
    pub units: Vec<String>,
    /// Manifest host roles (`keeper`, `worker`, `wsl`, `all`); empty without a manifest.
    pub roles: Vec<String>,
    /// Why the train does not install it (not deployed, a system unit, a rendered template).
    pub skip: Option<String>,
}

/// One commit of the range that touches a shipped path.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Touch {
    pub sha: String,
    pub merge: bool,
    pub items: BTreeSet<String>,
    pub hosts: BTreeSet<String>,
    /// Names of the families it touches.
    pub families: BTreeSet<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Derived {
    pub families: BTreeMap<String, Family>,
    pub touches: Vec<Touch>,
}

/// The unit file a changed path ships without a manifest (the keeper's rule).
pub(super) fn family_of(status: &str, path: &str) -> Option<String> {
    let rest = path.strip_prefix("deploy/systemd/")?;
    let unit = [".service", ".timer", ".socket", ".path"]
        .iter()
        .any(|s| rest.ends_with(s) && !rest.contains('/'));
    (unit && status != "D").then(|| rest.to_owned())
}

/// `bin:<name>` of an added binary entry point without a manifest (the keeper's rule).
pub(super) fn binary_of(status: &str, path: &str) -> Option<String> {
    if status != "A" {
        return None;
    }
    let (krate, tail) = path.strip_prefix("rust/")?.split_once('/')?;
    if tail == "src/main.rs" {
        return Some(format!("bin:{krate}"));
    }
    let bin = tail.strip_prefix("src/bin/")?.strip_suffix(".rs")?;
    (!bin.contains('/')).then(|| format!("bin:{bin}"))
}

/// Values of the `name:` trailer lines of a commit message, comma separated.
pub(super) fn trailers(message: &str, name: &str) -> Vec<String> {
    let want = format!("{}:", name.to_ascii_lowercase());
    message
        .lines()
        .filter(|l| l.to_ascii_lowercase().starts_with(&want))
        .flat_map(|l| l[want.len()..].split(','))
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .collect()
}

fn unit_family(entry: &UnitEntry) -> Family {
    let mut units: Vec<String> = entry
        .installed_enable_unit()
        .map(str::to_owned)
        .into_iter()
        .collect();
    units.extend(
        entry
            .installed_service_unit()
            .filter(|s| !units.contains(s)),
    );
    let skip = entry
        .not_deployed
        .as_ref()
        .map(|r| format!("not deployed: {r}"))
        .or_else(|| {
            (entry.scope == Scope::System)
                .then(|| "system unit: root installs it (operator step)".into())
        })
        .or_else(|| {
            entry
                .installer
                .as_ref()
                .map(|i| format!("rendered by {}: {}", i.via, i.step))
        })
        .or_else(|| {
            units
                .iter()
                .any(|u| is_template(u) || u.ends_with(".in"))
                .then(|| "template unit: its instances are host configuration".into())
        })
        .or_else(|| units.is_empty().then(|| "no unit to enable".into()));
    Family {
        name: entry.name.clone(),
        units,
        roles: entry.hosts.clone(),
        skip,
    }
}

/// `marked`: the binaries `host-sync --install-release` builds (the crates' host-binaries marks).
fn binary_family(entry: &BinaryEntry, marked: &BTreeSet<String>) -> Family {
    let name = format!("bin:{}", entry.bin);
    let outside = entry
        .install_paths
        .iter()
        .find(|p| !p.path.starts_with("~/"));
    let skip = entry
        .not_deployed
        .as_ref()
        .map(|r| format!("not deployed: {r}"))
        .or_else(|| {
            let home = entry.install_paths.is_empty()
                || entry.install_paths.iter().any(|p| p.path.starts_with("~/"));
            outside
                .filter(|_| !home)
                .map(|p| format!("installs to {}: operator step", p.path))
        });
    let skip = skip.or_else(|| {
        (!marked.contains(&entry.bin)).then(|| {
            format!(
                "{} does not mark it in [package.metadata.plangraph] host-binaries, so host-sync --install-release does not build it",
                entry.package
            )
        })
    });
    Family {
        name: name.clone(),
        units: vec![name],
        roles: entry.hosts.clone(),
        skip,
    }
}

/// The crates of a tree: directory under `rust/` -> package, and package -> every package that depends on
/// it by path (itself included).
#[derive(Clone, Debug, Default)]
pub struct Crates {
    pub dirs: BTreeMap<String, String>,
    pub users: BTreeMap<String, BTreeSet<String>>,
    /// Binaries marked `host-binaries` or `optional-host-binaries`.
    pub marked: BTreeSet<String>,
}

impl Crates {
    /// The binaries crates mark in `[package.metadata.plangraph] <key>`: `host-binaries`, or
    /// `optional-host-binaries` (installed only with `host-sync --install-release --with-keeper-backup`).
    pub fn marks(tomls: &BTreeMap<String, String>, key: &str) -> BTreeSet<String> {
        tomls
            .values()
            .filter_map(|t| t.parse::<toml::Table>().ok())
            .filter_map(|v| {
                let list = v
                    .get("package")?
                    .get("metadata")?
                    .get("plangraph")?
                    .get(key)?;
                Some(
                    list.as_array()?
                        .iter()
                        .filter_map(|b| b.as_str().map(str::to_owned))
                        .collect::<Vec<_>>(),
                )
            })
            .flatten()
            .collect()
    }

    /// From the `rust/<dir>/Cargo.toml` texts of a tree.
    pub fn parse(tomls: &BTreeMap<String, String>) -> Crates {
        let mut deps: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        let mut dirs = BTreeMap::new();
        for (dir, text) in tomls {
            let Ok(value) = text.parse::<toml::Table>() else {
                continue;
            };
            let Some(name) = value
                .get("package")
                .and_then(|p| p.get("name"))
                .and_then(|n| n.as_str())
            else {
                continue;
            };
            dirs.insert(dir.clone(), name.to_owned());
            let paths: BTreeSet<String> = ["dependencies", "build-dependencies"]
                .iter()
                .filter_map(|k| value.get(*k).and_then(|d| d.as_table()))
                .flat_map(|t| t.values())
                .filter_map(|d| d.get("path").and_then(|p| p.as_str()))
                .filter_map(|p| {
                    p.trim_end_matches('/')
                        .rsplit('/')
                        .next()
                        .map(str::to_owned)
                })
                .collect();
            deps.insert(dir.clone(), paths);
        }
        let mut users: BTreeMap<String, BTreeSet<String>> = dirs
            .values()
            .map(|p| (p.clone(), BTreeSet::from([p.clone()])))
            .collect();
        // A package's users are its direct users and theirs: grow until nothing changes.
        loop {
            let mut grew = false;
            for (dir, on) in &deps {
                let user = &dirs[dir];
                for dep in on.iter().filter_map(|d| dirs.get(d)) {
                    let set = users.get(user).cloned().unwrap_or_default();
                    let entry = users.entry(dep.clone()).or_default();
                    let before = entry.len();
                    entry.extend(set);
                    grew |= entry.len() != before;
                }
            }
            if !grew {
                let mut marked = Self::marks(tomls, "host-binaries");
                marked.extend(Self::marks(tomls, "optional-host-binaries"));
                return Crates {
                    dirs,
                    users,
                    marked,
                };
            }
        }
    }
}

/// What a tree deploys: its manifest (if any) and its crates.
pub struct Index {
    pub manifest: Option<Manifest>,
    pub crates: Crates,
}

impl Index {
    /// Read from the tree of `rev`.
    pub fn at(repo: &Path, rev: &str) -> Result<Index, String> {
        let files = git::out(repo, &["ls-tree", "-r", "--name-only", rev])?;
        let show = |p: &str| git::out(repo, &["show", &format!("{rev}:{p}")]);
        let manifest = match files.lines().any(|f| f == MANIFEST_PATH) {
            true => Some(Manifest::parse(&show(MANIFEST_PATH)?)?),
            false => None,
        };
        let mut tomls = BTreeMap::new();
        for f in files.lines() {
            if let Some(dir) = f
                .strip_prefix("rust/")
                .and_then(|r| r.strip_suffix("/Cargo.toml"))
            {
                if !dir.contains('/') {
                    tomls.insert(dir.to_owned(), show(f)?);
                }
            }
        }
        Ok(Index {
            manifest,
            crates: Crates::parse(&tomls),
        })
    }

    /// The families a path with net status `status` ships.
    pub fn families_for(&self, status: &str, path: &str) -> Vec<Family> {
        let Some(m) = &self.manifest else {
            return family_of(status, path)
                .or_else(|| binary_of(status, path))
                .map(|u| Family {
                    name: u.clone(),
                    units: vec![u],
                    roles: vec![],
                    skip: None,
                })
                .into_iter()
                .collect();
        };
        if status == "D" {
            return vec![];
        }
        let mut out: Vec<Family> = m
            .unit
            .iter()
            .filter(|u| {
                u.files
                    .iter()
                    .any(|f| path == format!("{}/{f}", u.source_dir()))
            })
            .map(unit_family)
            .collect();
        let dir = path
            .strip_prefix("rust/")
            .and_then(|r| r.split_once('/'))
            .map(|(d, _)| d);
        if let Some(users) = dir
            .and_then(|d| self.crates.dirs.get(d))
            .and_then(|p| self.crates.users.get(p))
        {
            out.extend(
                m.binary
                    .iter()
                    .filter(|b| users.contains(&b.package))
                    .map(|b| binary_family(b, &self.crates.marked)),
            );
        }
        out
    }
}

/// Net `(status, path)` of the range: what the head has against the base.
fn net_status(repo: &Path, base: &str, head: &str) -> Result<BTreeMap<String, String>, String> {
    let raw = git::bytes(
        repo,
        &[
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--name-status",
            "-z",
            base,
            head,
            "--",
        ],
    )?;
    let fields: Vec<String> = raw
        .split(|&b| b == 0)
        .filter(|f| !f.is_empty())
        .map(|f| String::from_utf8_lossy(f).into_owned())
        .collect();
    Ok(fields
        .chunks(2)
        .filter_map(|c| match c {
            [status, path] => Some((path.clone(), status.clone())),
            _ => None,
        })
        .collect())
}

/// One commit of a range: sha, whether it is a merge, message, the paths it changes.
type Logged = (String, bool, String, Vec<String>);

/// The commits of `base..head` with the paths each changes (a merge: the paths its resolution changed).
fn commits(repo: &Path, base: &str, head: &str) -> Result<Vec<Logged>, String> {
    let range = format!("{base}..{head}");
    let raw = git::bytes(
        repo,
        &[
            "log",
            "--cc",
            "--no-renames",
            "--name-only",
            "-z",
            "--format=%x1e%H %P%x1f%B%x1f",
            &range,
            "--",
        ],
    )?;
    let text = String::from_utf8_lossy(&raw);
    Ok(text
        .split('\u{1e}')
        .filter_map(|record| {
            let (head, names) = record.rsplit_once('\u{1f}')?;
            let (ids, body) = head.split_once('\u{1f}')?;
            let mut ids = ids.split_whitespace();
            let sha = ids.next()?.to_owned();
            let merge = ids.count() > 1;
            let paths = names
                .split('\0')
                .map(|n| n.trim_start_matches('\n'))
                .filter(|n| !n.is_empty())
                .map(str::to_owned)
                .collect();
            Some((sha, merge, body.to_owned(), paths))
        })
        .collect())
}

/// The families `base..head` ships and the commits that touch them, classified train-wide.
pub fn derive_range(repo: &Path, base: &str, head: &str) -> Result<Derived, String> {
    let index = Index::at(repo, head)?;
    let mut derived = Derived::default();
    let mut shipped: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (path, status) in net_status(repo, base, head)? {
        for family in index.families_for(&status, &path) {
            shipped
                .entry(path.clone())
                .or_default()
                .insert(family.name.clone());
            derived.families.insert(family.name.clone(), family);
        }
    }
    if shipped.is_empty() {
        return Ok(derived);
    }
    for (sha, merge, body, paths) in commits(repo, base, head)? {
        let families: BTreeSet<String> = paths
            .iter()
            .filter_map(|p| shipped.get(p))
            .flatten()
            .cloned()
            .collect();
        if !families.is_empty() {
            derived.touches.push(Touch {
                sha,
                merge,
                items: trailers(&body, "Plan-Item").into_iter().collect(),
                hosts: trailers(&body, "Deploy").into_iter().collect(),
                families,
            });
        }
    }
    // A shipped family no single commit names (an evil merge outside --cc) still ships, unattributed.
    let named: BTreeSet<&String> = derived
        .touches
        .iter()
        .flat_map(|t| t.families.iter())
        .collect();
    let orphans: BTreeSet<String> = derived
        .families
        .keys()
        .filter(|f| !named.contains(f))
        .cloned()
        .collect();
    if !orphans.is_empty() {
        derived.touches.push(Touch {
            sha: head.to_owned(),
            families: orphans,
            ..Touch::default()
        });
    }
    Ok(derived)
}
