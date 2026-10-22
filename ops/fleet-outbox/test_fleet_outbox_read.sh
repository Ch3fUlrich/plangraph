#!/bin/bash
# Hermetic test for ops/fleet-outbox/fleet-outbox-read (D-2221).
# Runs the forced command against a throwaway outbox inside a mktemp sandbox;
# never touches $HOME/fleet-outbox. Prints PASS/FAIL per assertion, exits
# nonzero if any FAIL.
set -u

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
READ="${FLEET_OUTBOX_READ:-$HERE/fleet-outbox-read}"

SANDBOX=$(mktemp -d "${TMPDIR:-/tmp}/fleet-outbox-read.XXXXXX") || exit 2
trap 'rm -rf -- "$SANDBOX"' EXIT

ROOT="$SANDBOX/outbox"
OUT="$SANDBOX/out"
FAKEHOME="$SANDBOX/home"
mkdir -p "$ROOT/workst/d" "$OUT" "$FAKEHOME"

printf 'SECRET\n' > "$ROOT/workst/d/tok"     # the allowed file
printf 'OUTSIDE\n' > "$OUT/secret"           # outside the outbox
ln -s "$OUT/secret" "$ROOT/workst/link"      # symlink -> outside file
ln -s "$OUT" "$ROOT/ld"                      # symlinked dir -> outside dir

printf 'SECRET\n' > "$SANDBOX/expect_ok"
printf 'refused\n' > "$SANDBOX/expect_refused"

fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
ok()   { if [ "$1" -eq 0 ]; then pass "$2"; else fail "$2"; fi; }

SO="$SANDBOX/stdout" SE="$SANDBOX/stderr" SC="$SANDBOX/status"
rc=0

# run CHILD_CMD  ->  child exit status in $rc, stdout/stderr captured.
# The child always gets FLEET_OUTBOX_ROOT pointing at the sandbox and a
# sandbox HOME, so the real $HOME/fleet-outbox can never be read or written.
run_cmd() {        # $1: value for SSH_ORIGINAL_COMMAND, or __UNSET__
	: > "$SO"; : > "$SE"
	if [ "$1" = __UNSET__ ]; then
		env -u SSH_ORIGINAL_COMMAND \
			FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME" \
			"$READ" > "$SO" 2> "$SE"
	else
		env SSH_ORIGINAL_COMMAND="$1" \
			FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME" \
			"$READ" > "$SO" 2> "$SE"
	fi
	rc=$?
}

stdout_is()     { cmp -s -- "$SO" "$1"; }     # 0 when stdout matches file exactly
stdout_empty()  { [ ! -s "$SO" ]; }
stderr_is_refused() { cmp -s -- "$SE" "$SANDBOX/expect_refused"; }

if [ ! -e "$READ" ]; then
	fail "setup read script exists at $READ"
	echo "FAIL count: $fails"
	exit 1
fi
if [ ! -x "$READ" ]; then
	fail "setup read script is executable"
	echo "FAIL count: $fails"
	exit 1
fi
pass "setup read script present and executable"

# --- 1. happy path: get workst/d/tok -> exactly "SECRET\n", exit 0 ----------
run_cmd 'get workst/d/tok'
st=0
[ "$rc" -eq 0 ] || st=1
stdout_is "$SANDBOX/expect_ok" || st=1
ok "$st" "1 get workst/d/tok serves SECRET with exit 0"

# --- 2. unset SSH_ORIGINAL_COMMAND ----------------------------------------
run_cmd __UNSET__
st=0
[ "$rc" -eq 1 ] || st=1
stdout_empty || st=1
ok "$st" "2 unset SSH_ORIGINAL_COMMAND refused, empty stdout"

# --- 3. shell / protocol / extra-arg commands refused ----------------------
shells=(
	'bash'
	'sh -c id'
	'/bin/sh'
	'get workst/d/tok; id'
	'get workst/d/tok && id'
	'get $(id)'
	'get `id`'
	'scp -f workst/d/tok'
	'rsync --server -e. . workst'
	'internal-sftp'
	'get workst/d/tok extra'
)
for c in "${shells[@]}"; do
	run_cmd "$c"
	st=0
	[ "$rc" -eq 1 ] || st=1
	stdout_empty || st=1
	stderr_is_refused || st=1
	ok "$st" "3 shell/protocol refused: [$c]"
done

# --- 4. escapes refused ----------------------------------------------------
escapes=(
	'get ../x'
	'get workst/../../OUT/secret'
	'get /etc/passwd'
	'get ./workst/d/tok'
	'get workst//d/tok'
	'get workst/d/'
	'get workst/link'
	'get ld/secret'
	'get workst/d'
	'get workst/nonexistent'
)
for c in "${escapes[@]}"; do
	run_cmd "$c"
	st=0
	[ "$rc" -eq 1 ] || st=1
	stdout_empty || st=1
	stderr_is_refused || st=1
	ok "$st" "4 escape refused: [$c]"
done

# --- 5. stderr on refusal is exactly "refused" (one line, no echo of input) -
refusals=0 bad_err=0
for c in "${shells[@]}" "${escapes[@]}"; do
	run_cmd "$c"
	[ "$rc" -eq 1 ] || continue
	refusals=$((refusals + 1))
	stderr_is_refused || bad_err=$((bad_err + 1))
done
st=0
[ "$refusals" -eq 21 ] || st=1
[ "$bad_err" -eq 0 ] || st=1
ok "$st" "5 stderr on refusal is exactly \"refused\" ($refusals refusals, $bad_err mismatched)"

if [ "$fails" -ne 0 ]; then
	echo "FAIL count: $fails"
	exit 1
fi
echo "ALL PASS"
exit 0
