//! Run named top-level definitions of a Python source that is compiled into this binary.
//!
//! The landing gates read the keeper registry and the GO row with the functions the batch land gate uses
//! (`schema_pg.registry_version`, `store_waiver.migration_for_go`), so the two gates share one parser. The
//! binary carries the text of those files; only the named assignments and functions are compiled and run in an
//! isolated interpreter (`-I`: no Python path, no site), so no checkout of `plangraph` is needed and no code of
//! the revision under test runs.
use std::io::Write;
use std::process::{Command, Stdio};

/// Reads `{"source", "names", "func", "args"}` from stdin, runs the named top-level `def`s and single-name
/// assignments of `source` (in file order) with `ast` and `re` in scope, calls `func(*args)` and prints its
/// JSON result; a `ValueError` becomes the exit message.
const LOADER: &str = "import ast, json, re, sys\nrequest = json.load(sys.stdin)\nwanted = set(request['names'])\nnodes = [n for n in ast.parse(request['source']).body if (isinstance(n, ast.FunctionDef) and n.name in wanted) or (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in wanted)]\nfound = {n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in nodes}\nif found != wanted:\n    sys.exit('the embedded reader lacks: ' + ', '.join(sorted(wanted - found)))\nnamespace = {'ast': ast, 're': re}\nexec(compile(ast.Module(nodes, []), 'embedded_reader', 'exec'), namespace)\ntry:\n    print(json.dumps(namespace[request['func']](*request['args'])))\nexcept ValueError as exc:\n    sys.exit(str(exc))\n";

/// `func(*args)` of the definitions `names` of `source`, as JSON.
pub fn call(
    python: &dyn Fn() -> Command,
    source: &str,
    names: &[&str],
    func: &str,
    args: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let request = serde_json::json!({"source": source, "names": names, "func": func, "args": args})
        .to_string();
    let mut child = python()
        .args(["-I", "-c", LOADER])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("the embedded reader is unavailable: {e}"))?;
    child
        .stdin
        .take()
        .ok_or("no stdin for the embedded reader")?
        .write_all(request.as_bytes())
        .map_err(|e| format!("the embedded reader: {e}"))?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_owned());
    }
    serde_json::from_slice(&out.stdout)
        .map_err(|e| format!("the embedded reader printed no JSON: {e}"))
}
