use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default = "default_prefix")]
    pub user_prefix: String,
    pub count: Option<u32>,
    #[serde(default = "default_home_base")]
    pub home_base: PathBuf,
    #[serde(default = "default_temporal")]
    pub temporal_test_address: String,
    #[serde(default = "default_packages")]
    pub apt_packages: Vec<String>,
    pub actions_runner: ActionsRunner,
    pub python_seed: PythonSeed,
}
#[derive(Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ActionsRunner {
    pub version: String,
    pub url: String,
    pub sha256: String,
}
#[derive(Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PythonSeed {
    pub version: String,
    pub url: String,
    pub size: u64,
    pub sha256: String,
    pub requirements_file: Option<PathBuf>,
    #[serde(default = "default_tool_cache")]
    pub tool_cache: PathBuf,
}

fn default_prefix() -> String {
    "runner".into()
}
fn default_home_base() -> PathBuf {
    "/home".into()
}
fn default_temporal() -> String {
    "127.0.0.1:1".into()
}
fn default_tool_cache() -> PathBuf {
    "/opt/hostedtoolcache".into()
}
fn default_packages() -> Vec<String> {
    [
        "protobuf-compiler",
        "libprotobuf-dev",
        "git",
        "curl",
        "build-essential",
    ]
    .map(String::from)
    .to_vec()
}

impl Config {
    pub fn load(path: &Path) -> Result<Config, String> {
        let text =
            std::fs::read_to_string(path).map_err(|e| format!("config {}: {e}", path.display()))?;
        Self::parse(&text)
    }
    pub fn parse(text: &str) -> Result<Config, String> {
        let config: Config = toml::from_str(text).map_err(|e| format!("config: {e}"))?;
        config.validate()?;
        Ok(config)
    }
    pub fn user_names(&self, count: u32) -> Vec<String> {
        (1..=count)
            .map(|n| format!("{}{n}", self.user_prefix))
            .collect()
    }
    pub fn home(&self, user: &str) -> PathBuf {
        self.home_base.join(user)
    }
    pub fn seed_dir(&self) -> PathBuf {
        self.python_seed.tool_cache.join("Python")
    }
    pub fn seed_python(&self) -> PathBuf {
        self.seed_dir()
            .join(&self.python_seed.version)
            .join("x64/bin/python")
    }
    fn validate(&self) -> Result<(), String> {
        let word = |s: &str| {
            !s.is_empty()
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        };
        let first_alpha = self
            .user_prefix
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase());
        if !first_alpha
            || !self
                .user_prefix
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
        {
            return Err("config: user_prefix must be [a-z][a-z0-9_]*".into());
        }
        if !word(&self.actions_runner.version) || !word(&self.python_seed.version) {
            return Err("config: versions may only hold letters, digits, . _ -".into());
        }
        for (name, sha) in [
            ("actions_runner.sha256", &self.actions_runner.sha256),
            ("python_seed.sha256", &self.python_seed.sha256),
        ] {
            if sha.len() != 64 || !sha.chars().all(|c| matches!(c, '0'..='9' | 'a'..='f')) {
                return Err(format!("config: {name} must be 64 lowercase hex digits"));
            }
        }
        for (name, url) in [
            ("actions_runner.url", &self.actions_runner.url),
            ("python_seed.url", &self.python_seed.url),
        ] {
            if !url.starts_with("https://") {
                return Err(format!("config: {name} must be an https URL"));
            }
        }
        if self.python_seed.size == 0 {
            return Err("config: python_seed.size must be set".into());
        }
        if self
            .apt_packages
            .iter()
            .any(|p| !word(p) && !p.contains('+'))
        {
            return Err("config: apt_packages holds an invalid package name".into());
        }
        if !self.home_base.is_absolute() || !self.python_seed.tool_cache.is_absolute() {
            return Err("config: home_base and tool_cache must be absolute".into());
        }
        if self.count == Some(0) || self.count.is_some_and(|c| c > 64) {
            return Err("config: count must be 1..=64".into());
        }
        Ok(())
    }
}

#[cfg(test)]
pub(crate) fn sample() -> Config {
    Config::parse(include_str!(
        "../../../../docs/operations/ci-runners.example.toml"
    ))
    .expect("example config parses")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn example_parses_with_defaults() {
        let c = sample();
        assert_eq!(c.user_names(2), ["runner1", "runner2"]);
        assert_eq!(c.apt_packages.len(), 5);
    }
    #[test]
    fn rejects_bad_sha_url_and_unknown_keys() {
        let ok = include_str!("../../../../docs/operations/ci-runners.example.toml");
        let bad_sha = ok.replacen(&sample().actions_runner.sha256, "ABC", 1);
        assert!(Config::parse(&bad_sha).unwrap_err().contains("sha256"));
        let bad_url = ok.replacen("https://", "http://", 1);
        assert!(Config::parse(&bad_url).unwrap_err().contains("https"));
        assert!(Config::parse(&format!("bogus = 1\n{ok}")).is_err());
    }
}
