//! Build identity for Temporal worker versioning.
//!
//! Versioning pins a running workflow to the build that started it: the worker
//! advertises a deployment name and a build id, and the server routes new
//! workflow tasks to workers running a compatible build. The id is therefore a
//! stable function of the binary, with an explicit runtime override so a
//! locally built binary can join a named deployment.

/// Default deployment name when `PG_DEPLOYMENT` is not set.
pub const DEPLOYMENT: &str = "pg-worker";

/// Build id for this binary: the runtime `PG_BUILD_ID` when set and non-empty,
/// otherwise `<package-version>-<git-sha>` stamped in by `build.rs`. The runtime
/// override lets an operator pin an otherwise-unnamed local build.
pub fn build_id() -> String {
    match std::env::var("PG_BUILD_ID") {
        Ok(v) if !v.is_empty() => v,
        _ => format!("{}-{}", env!("CARGO_PKG_VERSION"), env!("PG_BUILD_SHA")),
    }
}

/// Whether `id` is a build id Temporal will accept: 1..=63 characters from
/// `[A-Za-z0-9._-]`, starting with an alphanumeric. Temporal rejects anything
/// else, so we check before handing an id to the server.
pub fn validate(id: &str) -> bool {
    match id.chars().next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    id.len() <= 63
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// Require explicit, valid deployment/build identity before starting a worker.
pub fn from_env() -> Result<(String, String), String> {
    if std::env::var("PG_VERSIONING").as_deref() == Ok("off") {
        return Err("PG_VERSIONING=off is not permitted".into());
    }
    let required = |key| {
        let value = std::env::var(key).map_err(|_| format!("{key} is required"))?;
        if !validate(&value) {
            return Err(format!("{key} must be a valid non-empty identity"));
        }
        Ok(value)
    };
    Ok((required("PG_DEPLOYMENT")?, required("PG_BUILD_ID")?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// Serializes environment mutation: the environment is process-global and
    /// tests run on threads, so unguarded set/remove would race.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Set or clear an env var. Safe only while `ENV_LOCK` is held.
    fn set(key: &str, value: Option<&str>) {
        // SAFETY: every caller holds ENV_LOCK for the whole mutation, so this
        // module never races another test in the module on the environment.
        unsafe {
            match value {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
        }
    }

    /// Run `f` with `vars` applied (`None` clears), restoring previous values
    /// afterwards. Holds `ENV_LOCK` for the whole call, including `f`.
    fn with_env<T>(vars: &[(&str, Option<&str>)], f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let saved: Vec<(String, Option<String>)> = vars
            .iter()
            .map(|(k, _)| ((*k).to_string(), std::env::var(k).ok()))
            .collect();
        for (k, v) in vars {
            set(k, *v);
        }
        let out = f();
        for (k, v) in &saved {
            set(k, v.as_deref());
        }
        out
    }

    #[test]
    fn validate_accepts_well_formed_ids() {
        assert!(validate("a"));
        assert!(validate("0"));
        assert!(validate("0.1.0-deadbeef1234"));
        assert!(validate("build_id.1-2"));
        assert!(validate(&"a".repeat(63)));
    }

    #[test]
    fn validate_rejects_bad_ids() {
        assert!(!validate(""));
        assert!(!validate("has space"));
        assert!(!validate("a/b"));
        assert!(!validate(&"a".repeat(64)));
        assert!(!validate("-leading"));
        assert!(!validate(".leading"));
        assert!(!validate("_leading"));
        assert!(!validate("caf\u{e9}"));
    }

    #[test]
    fn build_id_default_uses_package_version() {
        let id = with_env(&[("PG_BUILD_ID", None)], build_id);
        assert_eq!(
            id,
            format!("{}-{}", env!("CARGO_PKG_VERSION"), env!("PG_BUILD_SHA"))
        );
        assert!(validate(&id));
    }

    #[test]
    fn build_id_env_override_wins_at_runtime() {
        let id = with_env(&[("PG_BUILD_ID", Some("override-1"))], build_id);
        assert_eq!(id, "override-1");
    }

    #[test]
    fn build_id_empty_env_falls_back_to_stamp() {
        let id = with_env(&[("PG_BUILD_ID", Some(""))], build_id);
        assert!(id.starts_with(concat!(env!("CARGO_PKG_VERSION"), "-")));
    }

    #[test]
    fn identity_is_mandatory_even_without_versioning_switch() {
        for deployment in [None, Some(""), Some("bad deployment"), Some("custom")] {
            for build in [None, Some(""), Some("bad id"), Some("id-1")] {
                let result = with_env(
                    &[("PG_VERSIONING", None), ("PG_DEPLOYMENT", deployment),
                      ("PG_BUILD_ID", build)], from_env);
                assert_eq!(result.is_ok(), deployment == Some("custom") && build == Some("id-1"));
            }
        }
        assert!(with_env(&[("PG_VERSIONING", Some("off")),
            ("PG_DEPLOYMENT", Some("custom")), ("PG_BUILD_ID", Some("id-1"))], from_env).is_err());
    }
}
