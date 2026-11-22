#![cfg(unix)]

use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

#[test]
fn sccache_environment_uses_one_host_cache_and_falls_back_when_missing() {
    let home = tempfile::tempdir().unwrap();
    let tools = home.path().join("bin");
    fs::create_dir(&tools).unwrap();
    let cache = tools.join("sccache");
    fs::write(&cache, "fixture").unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o755)).unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = Command::new("python3")
        .env("PYTHONPATH", source)
        .env("HOME", home.path())
        .env_remove("RUSTC_WRAPPER")
        .env_remove("SCCACHE_DIR")
        .env_remove("PLANGRAPH_SCCACHE")
        .env_remove("PLANGRAPH_SCCACHE_CACHE_SIZE")
        .env_remove("SCCACHE_CACHE_SIZE")
        .arg("-c")
        .arg(
            r#"
import os, sys
from pathlib import Path
from unittest.mock import patch
from plangraph.spawner.host_ready import cargo_child_env, main

home = Path.home()
base = dict(PATH=sys.argv[1])
wrapped = cargo_child_env(base)
assert wrapped['RUSTC_WRAPPER'] == str(home/'bin/sccache')
assert wrapped['SCCACHE_DIR'] == str(home/'.cache/plangraph/sccache')
assert wrapped['SCCACHE_CACHE_SIZE'] == '20G'
assert wrapped['CARGO_INCREMENTAL'] == '0'
assert cargo_child_env(dict(base, PLANGRAPH_SCCACHE_CACHE_SIZE='5G'))['SCCACHE_CACHE_SIZE'] == '5G'
output = home/'github-env'
with patch.dict(os.environ, base):
    assert main(['--cargo-cache-env', str(output)]) == 0
settings = dict(line.split('=', 1) for line in output.read_text().splitlines())
assert settings == {key: wrapped[key] for key in (
    'RUSTC_WRAPPER', 'SCCACHE_DIR', 'SCCACHE_CACHE_SIZE', 'CARGO_INCREMENTAL')}
missing = dict(PATH=str(home/'missing'), RUSTC_WRAPPER='sccache')
for _ in range(2):
    assert cargo_child_env(missing).get('RUSTC_WRAPPER', '') == ''
assert os.environ.get('RUSTC_WRAPPER') != wrapped['RUSTC_WRAPPER']
"#,
        )
        .arg(&tools)
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(output.status.success(), "{stderr}");
    assert_eq!(stderr.matches("sccache unavailable").count(), 1, "{stderr}");
}
