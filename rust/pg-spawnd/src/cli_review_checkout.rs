//! Disposable checkout and headless command permissions for native agy reviews.
use super::{clean_head, git, json, parameter, LaunchSpec, ToolError, Value};

/// Keep the checkout alive until the finish hook has checked provenance and
/// prepared any quota fallthrough. Dropping it also cleans up failed launches.
pub(crate) fn isolate(
    launch: &mut LaunchSpec,
    leg: &Value,
    head: Option<&str>,
) -> Result<Option<tempfile::TempDir>, ToolError> {
    let Some(head) = head.filter(|_| leg["harness"] == "agy") else {
        return Ok(None);
    };
    let clone = tempfile::Builder::new()
        .prefix("plangraph-agy-review-")
        .tempdir()
        .map_err(|_| parameter("could not create review checkout"))?;
    let source = launch.cwd.to_string_lossy().into_owned();
    let destination = clone.path().to_string_lossy().into_owned();
    git(
        &launch.cwd,
        &[
            "clone",
            "--shared",
            "--no-checkout",
            "--",
            &source,
            &destination,
        ],
    )?;
    git(clone.path(), &["checkout", "--detach", head])?;
    git(clone.path(), &["remote", "remove", "origin"])?;
    if clean_head(clone.path())? != head || !git(clone.path(), &["remote"])?.is_empty() {
        return Err(parameter("review clone does not match stamped HEAD"));
    }
    // D-6054: headless permission globs cannot reliably constrain commands.
    // Allow commands only after switching to a remote-free disposable clone.
    let settings = launch
        .argv
        .iter()
        .rposition(|s| s == "--settings")
        .filter(|i| i + 1 < launch.argv.len())
        .ok_or_else(|| parameter("missing agy review permissions"))?;
    launch.argv[settings + 1] = json!({"permissions":{"allow":[
        "command(*)", "read_file(*)", "list_dir(*)", "grep_search(*)", "find_by_name(*)"
    ]}})
    .to_string();
    // Do not grant the child access to the original checkout via add-dir.
    let mut argv = launch.argv.drain(..);
    let mut isolated = Vec::new();
    while let Some(arg) = argv.next() {
        if arg == "--add-dir" {
            argv.next();
        } else if !arg.starts_with("--add-dir=") {
            isolated.push(arg);
        }
    }
    drop(argv);
    launch.argv = isolated;
    launch.cwd = clone.path().into();
    Ok(Some(clone))
}
