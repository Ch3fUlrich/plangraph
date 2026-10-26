#!/usr/bin/env bash
# Run from the checkout root. Explicit sample selection leaves the worktree untouched.
set -euo pipefail
mode=${1:-both}
base=${2:-HEAD}
echo "host nproc: $(nproc)"
measure() {
  python3 -c 'import subprocess,sys,time; start=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]} wall seconds: {time.monotonic()-start:.3f}", flush=True); sys.exit(result.returncode)' "$@"
}
case "$mode" in affected|full|both) ;; *) echo "usage: $0 [affected|full|both] [base]" >&2; exit 2;; esac
if [[ "$mode" != full ]]; then
  measure affected python3 tools/affected_tests.py \
    --base "$base" --changed src/plangraph/spawner/runner.py --run -n auto
fi
if [[ "$mode" != affected ]]; then
  systemd-run --user --scope -p MemoryMax=6G \
    python3 -c 'import subprocess,sys,time; start=time.monotonic(); result=subprocess.run([sys.executable,"-m","pytest","-q","-n","auto","tests"]); print(f"full -n auto wall seconds: {time.monotonic()-start:.3f}", flush=True); sys.exit(result.returncode)'
fi
