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
sets `batch`. `auto` is accepted as a setting; selection and UI are later slices.
See the [schema](schemas/work-mode.schema.json) and
[example](schemas/examples/work-mode.json).

