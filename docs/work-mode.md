# Work-mode configuration

| Config key | Values | Default | Files (highest precedence first) |
| --- | --- | --- | --- |
| `work_mode` | `auto`, `piecewise`, `batch` | `batch` | `<repo-root>/.plangraph/work-mode.json`, `$XDG_CONFIG_HOME/plangraph/work-mode.json` |

D-5952 / rule 3.41: `pg_worker_core::work_mode::WorkMode` is an extensible
setting, loaded by `pg-spawnd`'s `work_mode_config::configured_mode(repo_root)`.
The caller supplies the repository root; the loader does not search ancestors.
The user path reuses the spawner's config-root lookup: when `XDG_CONFIG_HOME`
is unset it uses `~/.config/plangraph` on Unix or `%APPDATA%/plangraph` on Windows.
Only absent files fall through to the next source; unreadable files, malformed
JSON, missing/unknown keys and unknown values in the selected file return an
error with its path. A valid repository file overrides even an invalid user file.
If both files are absent, the setting is `batch`. This repository explicitly
sets `batch`. `auto` resolves from served writer legs in route output; see [routing](routing.md#automatic-work-mode-d-5952--rule-341).
See the [schema](schemas/work-mode.schema.json) and
[example](schemas/examples/work-mode.json).

The Settings tab reads and writes the user preference through the Rust
`plangraph_work_mode` MCP tool. Omit `work_mode` to read; supply `auto`,
`piecewise` or `batch` to save atomically. Optional `repo` selects the absolute
repository root. Responses distinguish the user setting, effective repository
setting, resolved mode and reason. Invalid requests leave the file unchanged.
An unavailable auto route is reported explicitly, without guessing a mode.

For the exported HTML, run `pg-spawnd settings-web --page plan.html --repo . --port 0`
and open the loopback URL it prints. The Rust host serves that export and forwards
same-origin JSON calls at `/mcp` to the same settings tool, binding the repository
to `--repo`. It exposes no other tool, file or credential interface. Opening the
export directly remains supported; work-mode changes require this local host.
The select shows repository overrides and auto's resolution and reason; failed
saves retain the last confirmed selection. Refresh reloads external config changes.

