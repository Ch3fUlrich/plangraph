use pg_ctx::dup_gates::{check, parse};
use std::{fs, path::Path};
const REGISTRY: &str = "[[capability]]\nname='token'\ncanonical='pkg.auth'\nlanguage='python'\nforbidden_defs=['read_token']\n";
fn put(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}
fn tree() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    put(dir.path(), "capabilities.toml", REGISTRY);
    put(dir.path(), "capabilities-baseline.toml", "[files]\n");
    put(dir.path(), "pkg/auth.py", "def read_token(): pass\n");
    dir
}
fn reject(root: &Path, base: &str, message: &str) {
    assert!(check(root, base).unwrap_err().contains(message));
}
#[test]
fn copied_definition_fails() {
    let dir = tree();
    put(dir.path(), "copy.py", "def read_token(): pass\n");
    reject(dir.path(), "[files]\n", "copy.py:1:");
}
#[test]
fn canonical_definition_and_import_pass() {
    let dir = tree();
    put(dir.path(), "caller.py", "from pkg.auth import read_token as token\n");
    put(dir.path(), "pkg/caller.py", "from .auth import (\n read_token,\n)\n");
    check(dir.path(), "[files]\n").unwrap();
}
#[test]
fn noncanonical_import_fails() {
    for import in ["from bad import read_token as t", "from bad import (\n read_token,\n)", "import bad.read_token"] {
        let dir = tree();
        put(dir.path(), "caller.py", &format!("# comment\n{import}\n"));
        reject(dir.path(), "[files]\n", "caller.py:2:");
    }
}
#[test]
fn baseline_covered_extra_and_stale_hits() {
    let dir = tree();
    let base = "[files]\n'copy.py'=1\n";
    put(dir.path(), "capabilities-baseline.toml", base);
    put(dir.path(), "copy.py", "def read_token(): pass\n");
    check(dir.path(), base).unwrap();
    put(dir.path(), "copy.py", "def read_token(): pass\ndef read_token(): pass\n");
    reject(dir.path(), base, "copy.py:2:");
    fs::remove_file(dir.path().join("copy.py")).unwrap();
    reject(dir.path(), base, "stale baseline");
    put(dir.path(), "capabilities-baseline.toml", "[files]\n");
    check(dir.path(), base).unwrap();
}
#[test]
fn baseline_cannot_grow_or_add_entries() {
    let dir = tree();
    put(dir.path(), "capabilities-baseline.toml", "[files]\n'copy.py'=2\n");
    for base in ["[files]\n", "[files]\n'copy.py'=1\n"] {
        reject(dir.path(), base, "only shrink");
    }
}
#[test]
fn toml_errors_and_missing_canonical_file() {
    for (text, error) in [
        (REGISTRY.replace("canonical='pkg.auth'\n", ""), "canonical"),
        (REGISTRY.replace("pkg.auth", ""), "canonical"),
        (REGISTRY.replace("python", "unknown"), "unknown language"),
        (format!("{REGISTRY}\n{REGISTRY}"), "duplicate"),
        ("not toml".into(), "capabilities.toml"),
    ] {
        assert!(parse(&text).unwrap_err().contains(error));
    }
    let dir = tree();
    fs::remove_file(dir.path().join("pkg/auth.py")).unwrap();
    reject(dir.path(), "[files]\n", "canonical module missing");
    put(dir.path(), "capabilities-baseline.toml", "[files]\n'x.py'=0\n");
    reject(dir.path(), "[files]\n", "zero count");
}
#[test]
fn rust_and_javascript_definitions_and_imports() {
    for (lang, ext, definition, import) in [
        ("rust", "rs", "pub async fn token() {}", "use wrong::{token as t};"),
        ("javascript", "js", "export function token() {}", "import {token as t} from 'wrong';"),
        ("typescript", "ts", "function token() {}", "import {token} from 'wrong';"),
    ] {
        let dir = tree();
        let registry = REGISTRY.replace("python", lang).replace("read_token", "token");
        put(dir.path(), "capabilities.toml", &registry);
        put(dir.path(), &format!("pkg/auth.{ext}"), definition);
        let good = if lang == "rust" { "use pkg::auth::token;" } else { "import {token} from 'pkg/auth';" };
        put(dir.path(), &format!("caller.{ext}"), good);
        check(dir.path(), "[files]\n").unwrap();
        put(dir.path(), &format!("copy.{ext}"), &format!("{definition}\n{import}\n"));
        reject(dir.path(), "[files]\n", &format!("copy.{ext}:2:"));
    }
}
