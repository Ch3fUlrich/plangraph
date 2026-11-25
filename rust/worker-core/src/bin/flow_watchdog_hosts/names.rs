//! Host identity: a snapshot row may carry any configured alias, alert keys carry the canonical id.
use pg_worker_core::hosts::HostRegistry;
use std::path::PathBuf;

pub struct Names {
    registry: Option<HostRegistry>,
    local: Option<String>,
}

fn registry_path() -> Option<PathBuf> {
    let var = |name| std::env::var_os(name).filter(|value| !value.is_empty());
    var("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| var("HOME").map(|home| PathBuf::from(home).join(".config")))
        .map(|config| config.join("plangraph/hosts.json"))
}

impl Names {
    /// The local host is `PLANGRAPH_HOST_NAME`, else the registry's `self` entry. The kernel
    /// hostname is no source: it need not be any name a collector labels its rows with.
    pub fn load() -> Self {
        let registry = registry_path().and_then(|path| HostRegistry::load(&path).ok());
        let mut names = Self {
            registry,
            local: None,
        };
        let configured = std::env::var("PLANGRAPH_HOST_NAME")
            .ok()
            .filter(|name| !name.is_empty())
            .or_else(|| Some(names.registry.as_ref()?.self_host.clone()));
        names.local = configured.map(|name| names.canonical(&name));
        names
    }

    /// The registry's canonical id for an id or alias; an unknown name stays as written.
    pub fn canonical(&self, name: &str) -> String {
        self.registry
            .as_ref()
            .and_then(|registry| registry.resolve(name))
            .map_or_else(|| name.to_owned(), |host| host.id.clone())
    }

    pub fn is_local(&self, canonical: &str) -> bool {
        self.local.as_deref() == Some(canonical)
    }
}
