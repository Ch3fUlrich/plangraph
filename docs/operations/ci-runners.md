# Self-hosted CI runners: Linux host setup

`plangraph runner` sets up the Linux side of a dedicated CI host (a WSL or plain Linux distro, run as root inside it). Every value that names a host, distro, user home or label is configuration, never code.

## Commands

- `plangraph runner plan --os linux --count N [--config FILE]` observes the host and prints the ordered action list. An empty list prints `nothing to do`. It changes nothing.
- `plangraph runner install --os linux --count N [--config FILE]` applies the same list as root. A second run on a finished host applies nothing.
- `plangraph runner check [--count N] [--config FILE]` runs the self-checks per runner user and exits 1 when any check fails. A missing temporal-cli binary is a warning only: setup-temporal downloads it during the first workflow run.

The config defaults to `~/.config/plangraph/ci-runners.toml`; see `docs/operations/ci-runners.example.toml` for every key (placeholder values only).

## What install does

1. apt packages from `apt_packages` (default protobuf-compiler, libprotobuf-dev, git, curl, build-essential), waiting on the dpkg lock.
2. Users `<user_prefix>1..N` with lingering, and `/etc/sudoers.d/ci-apt` limited to `/usr/bin/apt-get` and `/usr/bin/apt`, checked with `visudo -cf` before it is installed.
3. The Actions runner tarball (`[actions_runner]` url + sha256) downloaded once into `/var/cache/plangraph-runner`, verified, unpacked into each `~/runner`. The runners are not registered: they are single-use JIT runners (S2).
4. The Python tool-cache seed (`[python_seed]` url + size + sha256) verified before `setup.sh` runs, `requirements_file` preinstalled, then the tree is root-owned and read-only. It is re-seeded when the pinned sha or the requirements file changes.
5. Per user: a writable `~/hostedtoolcache` with a `Python` symlink to the seed, Playwright chromium (needs playwright in the requirements file), the `~/.cache/temporal-cli` symlink to the runner cache, and `~/run-one.sh`.
6. Once: `playwright install-deps chromium`.

`run-one.sh` exports the tool-cache variables, installs rustup when missing, reads and deletes `~/jit`, runs one job with `run.sh --jitconfig`, and cleans `_work`. The single-job JIT token is passed as `run.sh --jitconfig` (GitHub's only interface), so it is visible in the runner process arguments to users of the shared host until the job ends. `temporal_test_address` must be `host:port`, apt package names must match `[a-z0-9][a-z0-9+.-]*`, and version segments may not be dot-only. It never sets `CARGO_TARGET_DIR`: the workflow owns it.

A checksum or size mismatch refuses: a download is kept only when it matches the pin, and a cached file with another sha256 stops the plan.

## Config keys

`user_prefix` (default `runner`), `count`, `home_base` (default `/home`), `temporal_test_address` (default `127.0.0.1:1`), `apt_packages`; `[actions_runner]` `version`, `url`, `sha256`; `[python_seed]` `version`, `url`, `size`, `sha256`, `requirements_file`, `tool_cache` (default `/opt/hostedtoolcache`).

## Not in S1 (slice S2)

- Windows side: `.wslconfig`, distro import, `/etc/wsl.conf`, the supervisor and its logon start, and `--os windows`.
- JIT config fetching, repository variables (`CI_SELF_HOSTED` and the label variables), the trial dispatch on a branch.
- The Settings panel in the app.
