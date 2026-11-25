//! The keeper Postgres schema version of a git revision, read from the frozen tree.
//!
//! A train that raises it cannot be started by a plain unit restart: the runtime role has no DDL and the
//! keeper refuses a database behind its code. The GO must carry `MIGRATION <version>` and the deploy goes
//! through `plangraph keeper-restart` (LE-KEEPER-SCHEMA-TRAIN-LANDS-WITHOUT-MIGRATION-FIELD).
//!
//! The registry is read by one reader, `registry_version` of `schema_pg.py` (a Python AST read that never
//! executes the file), the same function the batch land gate calls, so the two gates cannot disagree about
//! what the registry is. The binary carries its own copy of that file's text (compiled in, like the workflow
//! pin of `go_verify`) and runs only that function in an isolated interpreter, so it needs no checkout of
//! `plangraph` on the Python path and never runs code of the revision under test.
use super::git;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

/// The file whose `MIGRATIONS` registry defines the version.
pub const SCHEMA_PATH: &str = "src/plangraph/keeper/schema_pg.py";

/// The text of that file as built into this binary; only its `registry_version` function is run.
const READER_SOURCE: &str = include_str!("../../../../src/plangraph/keeper/schema_pg.py");

/// Reads `{"reader": <file text>, "text": <registry text>}` from stdin, runs `registry_version` of the first
/// on the second and prints the version, or exits with the reason.
const LOADER: &str = "import ast, json, sys\nrequest = json.load(sys.stdin)\nnode = next(n for n in ast.parse(request['reader']).body if isinstance(n, ast.FunctionDef) and n.name == 'registry_version')\nnamespace = {'ast': ast}\nexec(compile(ast.Module([node], []), 'registry_version', 'exec'), namespace)\ntry:\n    print(namespace['registry_version'](request['text']))\nexcept ValueError as exc:\n    sys.exit(str(exc))\n";

/// The version of the registry in `text` (the contents of `SCHEMA_PATH`), asked of the shared reader.
pub fn parse_latest(python: &dyn Fn() -> Command, text: &str) -> Result<u32, String> {
    let request = serde_json::json!({"reader": READER_SOURCE, "text": text}).to_string();
    let mut child = python()
        .args(["-I", "-c", LOADER])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("the keeper registry reader is unavailable: {e}"))?;
    child
        .stdin
        .take()
        .ok_or("no stdin for the registry reader")?
        .write_all(request.as_bytes())
        .map_err(|e| format!("the keeper registry reader: {e}"))?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_owned());
    }
    String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<u32>()
        .map_err(|e| format!("the keeper registry reader printed no version: {e}"))
}

/// The keeper schema version at `rev`; 0 when the revision exists and has no schema file. A revision that
/// is not a commit of this repository is an error: a bad `--base` or `--head` must not read as version 0.
pub fn at(repo: &Path, rev: &str, python: &dyn Fn() -> Command) -> Result<u32, String> {
    git::out(
        repo,
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("{rev}^{{commit}}"),
        ],
    )
    .map_err(|_| format!("{rev} is not a commit of this repository"))?;
    let spec = format!("{rev}:{SCHEMA_PATH}");
    if !git::run(repo, None, &["cat-file", "-e", &spec])?
        .status
        .success()
    {
        return Ok(0);
    }
    let text = String::from_utf8_lossy(&git::bytes(repo, &["show", &spec])?).into_owned();
    parse_latest(python, &text)
        .map_err(|e| format!("keeper migration version is unreadable at {rev}: {e}"))
}

/// The head version when `head` is above `base`; `None` when it is equal. A lower head is an error.
pub fn rise(
    repo: &Path,
    base: &str,
    head: &str,
    python: &dyn Fn() -> Command,
) -> Result<Option<u32>, String> {
    let (before, after) = (at(repo, base, python)?, at(repo, head, python)?);
    if after < before {
        return Err(format!(
            "keeper migration regressed from {before} to {after}"
        ));
    }
    Ok((after > before).then_some(after))
}

/// `land-train keeper-schema --repo DIR --base SHA --head SHA [--go-statement TEXT --pr N --slug OWNER/NAME]`:
/// the two evidence fields of a train as one JSON line, `{"keeper_schema": {"base": N, "head": M},
/// "migration": K|null}`. The versions come from the pinned revisions; `migration` is the field of the GO
/// statement, which must name this exact pull request and head or the command refuses.
pub(super) fn run_cli(
    flags: &std::collections::BTreeMap<String, String>,
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let repo = Path::new(need("repo")?);
    let head_rev = need("head")?;
    let (base, head) = (
        at(repo, need("base")?, python)?,
        at(repo, head_rev, python)?,
    );
    let migration = match flags.get("go-statement") {
        Some(text) => {
            let pr = need("pr")?
                .parse::<u64>()
                .map_err(|e| format!("--pr: {e}"))?;
            let full = git::out(repo, &["rev-parse", &format!("{head_rev}^{{commit}}")])?;
            super::go_statement::migration_for(text, need("slug")?, pr, &full)?
        }
        None => None,
    };
    Ok(vec![serde_json::json!({
        "keeper_schema": {"base": base, "head": head},
        "migration": migration,
    })
    .to_string()])
}
