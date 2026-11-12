//! gix object/ref operations. Local transport never starts git-upload-pack.
use super::Alarm;
use gix::protocol::transport::{self, client::blocking_io::Transport, Protocol};
use std::{collections::BTreeSet, fs, io, path::Path, sync::atomic::AtomicBool};

fn options() -> gix::open::Options {
    gix::open::Options::isolated()
        .open_path_as_is(true)
        .config_overrides(["core.logAllRefUpdates=false"])
}

pub(super) fn open(path: &Path) -> Result<gix::Repository, Alarm> {
    gix::open_opts(path, options()).map_err(|_| Alarm::FetchFailed)
}

pub(super) fn init(path: &Path) -> Result<(), Alarm> {
    gix::ThreadSafeRepository::init_opts(
        path,
        gix::create::Kind::Bare,
        gix::create::Options::default(),
        options(),
    )
    .map_err(|_| Alarm::FetchFailed)?;
    Ok(())
}

pub(super) fn main_head(repo: &gix::Repository) -> Result<gix::ObjectId, Alarm> {
    let id = repo
        .find_reference("refs/heads/main")
        .and_then(|mut r| r.peel_to_id().map(|id| id.detach()))
        .map_err(|_| Alarm::FetchFailed)?;
    repo.find_commit(id).map_err(|_| Alarm::FetchFailed)?;
    Ok(id)
}

pub(super) fn set_main(repo: &gix::Repository, head: gix::ObjectId) -> Result<(), Alarm> {
    repo.reference(
        "refs/heads/main",
        head,
        gix::refs::transaction::PreviousValue::Any,
        "mirror accepted",
    )
    .map_err(|_| Alarm::FetchFailed)?;
    Ok(())
}

pub(super) fn fetch(repo: &gix::Repository, remote: &str) -> Result<gix::ObjectId, Alarm> {
    fetch_with_connector(repo, remote, |url, version| {
        transport::client::blocking_io::connect::connect(
            url,
            transport::client::blocking_io::connect::Options {
                version,
                ..Default::default()
            },
        )
    })
}

// Only connection establishment is replaceable: tests run the same refspec,
// credential refusal, negotiation and pack/ref receive code over local pipes.
pub(super) fn fetch_with_connector(
    repo: &gix::Repository,
    remote: &str,
    connect: impl FnOnce(gix::Url, Protocol) -> gix::Result<Box<dyn Transport + Send>>,
) -> Result<gix::ObjectId, Alarm> {
    let url = gix::url::parse(remote).map_err(|_| Alarm::FetchFailed)?;
    match url.scheme {
        gix::url::Scheme::File => {
            let path = gix::path::from_bstr(&url.path).map_err(|_| Alarm::FetchFailed)?;
            let source = open(path.as_ref())?;
            let head = main_head(&source)?;
            transfer(&source, repo, head)?;
            Ok(head)
        }
        // These transports run in-process. SSH/ext/file network transports
        // would start helpers, so never pass them to gix's connection API.
        gix::url::Scheme::Http | gix::url::Scheme::Https | gix::url::Scheme::Git => {
            let remote = repo
                .remote_at_without_url_rewrite(url)
                .and_then(|r| {
                    r.with_refspecs(
                        ["+refs/heads/main:refs/pg-ctx/main"],
                        gix::remote::Direction::Fetch,
                    )
                })
                .map_err(|_| Alarm::FetchFailed)?
                .with_fetch_tags(gix::remote::fetch::Tags::None);
            let (url, version) = remote
                .sanitized_url_and_version(gix::remote::Direction::Fetch)
                .map_err(|_| Alarm::FetchFailed)?;
            let transport = connect(url, version).map_err(|_| Alarm::FetchFailed)?;
            remote
                .to_connection_with_transport(transport)
                // Never fall back to credential helper programs or prompts.
                .with_credentials(|_| Ok(None))
                .prepare_fetch(gix::progress::Discard, Default::default())
                .and_then(|p| p.receive(gix::progress::Discard, &AtomicBool::new(false)))
                .map_err(|_| Alarm::FetchFailed)?;
            let id = repo
                .find_reference("refs/pg-ctx/main")
                .and_then(|mut r| r.peel_to_id().map(|id| id.detach()))
                .map_err(|_| Alarm::FetchFailed)?;
            repo.find_commit(id).map_err(|_| Alarm::FetchFailed)?;
            Ok(id)
        }
        _ => Err(Alarm::FetchFailed),
    }
}

fn transfer(
    source: &gix::Repository,
    target: &gix::Repository,
    head: gix::ObjectId,
) -> Result<(), Alarm> {
    use gix::objs::{Exists, Write};
    let mut pending = vec![head];
    let mut seen = BTreeSet::new();
    while let Some(id) = pending.pop() {
        // Existing reachable subgraphs are already complete; no old data I/O.
        if !seen.insert(id) || target.objects.exists(&id) {
            continue;
        }
        let object = source.find_object(id).map_err(|_| Alarm::FetchFailed)?;
        match object.kind {
            gix::objs::Kind::Commit => {
                let commit = source.find_commit(id).map_err(|_| Alarm::FetchFailed)?;
                let decoded = commit.decode().map_err(|_| Alarm::FetchFailed)?;
                pending.push(decoded.tree());
                pending.extend(decoded.parents());
            }
            gix::objs::Kind::Tree => {
                let tree = source.find_tree(id).map_err(|_| Alarm::FetchFailed)?;
                for entry in tree.iter() {
                    let entry = entry.map_err(|_| Alarm::FetchFailed)?;
                    // Gitlinks name commits in another repository.
                    if !entry.mode().is_commit() {
                        pending.push(entry.object_id());
                    }
                }
            }
            gix::objs::Kind::Blob => {}
            gix::objs::Kind::Tag => return Err(Alarm::FetchFailed),
        }
        let written = target
            .objects
            .write_buf(object.kind, &object.data)
            .map_err(|_| Alarm::FetchFailed)?;
        if written != id {
            return Err(Alarm::FetchFailed);
        }
    }
    Ok(())
}

pub(super) fn copy_repository(source: &Path, destination: &Path) -> io::Result<()> {
    fn copy(source: &Path, destination: &Path, relative: &Path) -> io::Result<()> {
        if !fs::symlink_metadata(source)?.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "repository directory must not be a symlink",
            ));
        }
        fs::create_dir(destination)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            let relative = relative.join(entry.file_name());
            let target = destination.join(entry.file_name());
            let kind = entry.file_type()?;
            if kind.is_dir() {
                copy(&entry.path(), &target, &relative)?;
            } else if !kind.is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "repository symlink or special file refused",
                ));
            } else if immutable_object(&relative) {
                // Same-filesystem quarantine: fail closed rather than silently
                // regress to a whole-object-database copy if linking fails.
                fs::hard_link(entry.path(), target)?;
            } else {
                fs::copy(entry.path(), target)?;
            }
        }
        Ok(())
    }
    copy(source, destination, Path::new(""))
}

fn immutable_object(path: &Path) -> bool {
    let parts: Vec<_> = path.iter().filter_map(|s| s.to_str()).collect();
    let hex = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_hexdigit());
    match parts.as_slice() {
        ["objects", fanout, name] if fanout.len() == 2 && hex(fanout) => {
            matches!(name.len(), 38 | 62) && hex(name)
        }
        ["objects", "pack", name] => name
            .strip_prefix("pack-")
            .and_then(|s| s.rsplit_once('.'))
            .is_some_and(|(id, ext)| {
                matches!(id.len(), 40 | 64)
                    && hex(id)
                    && matches!(ext, "pack" | "idx" | "rev" | "bitmap")
            }),
        _ => false,
    }
}
