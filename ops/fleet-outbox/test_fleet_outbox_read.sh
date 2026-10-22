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
ln -s "$OUT" "$ROOT/workst/mid"              # symlinked dir, middle segment
ln -s "$ROOT" "$SANDBOX/rootlink"            # ROOT itself as a symlink

printf 'SECRET\n' > "$SANDBOX/expect_ok"
printf 'refused\n' > "$SANDBOX/expect_refused"

# relpath of exactly 256 characters (the limit is 255).
LONG256=$(printf 'a%.0s' {1..256})

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

# run_in EXPECT_STATUS -- env-under-test is passed by the caller through
# $EXTRA_OPT (env options such as -u, which must precede assignments) and
# $EXTRA_ENV (NAME=VALUE assignments); same captures.
EXTRA_OPT=()
EXTRA_ENV=()
run_env() {        # $1: value for SSH_ORIGINAL_COMMAND, or __UNSET__
	: > "$SO"; : > "$SE"
	if [ "$1" = __UNSET__ ]; then
		env "${EXTRA_OPT[@]}" -u SSH_ORIGINAL_COMMAND "${EXTRA_ENV[@]}" \
			"$READ" > "$SO" 2> "$SE"
	else
		env "${EXTRA_OPT[@]}" "${EXTRA_ENV[@]}" SSH_ORIGINAL_COMMAND="$1" \
			"$READ" > "$SO" 2> "$SE"
	fi
	rc=$?
}

stdout_is()     { cmp -s -- "$SO" "$1"; }     # 0 when stdout matches file exactly
stdout_empty()  { [ ! -s "$SO" ]; }
stderr_is_refused() { cmp -s -- "$SE" "$SANDBOX/expect_refused"; }
stderr_empty()  { [ ! -s "$SE" ]; }

# refused check: exit 1, empty stdout, stderr byte-exactly "refused"
is_refused() { [ "$rc" -eq 1 ] && stdout_empty && stderr_is_refused; }
# served check: exit 0, stdout byte-exactly the file, silent stderr
is_served() { [ "$rc" -eq 0 ] && stdout_is "$SANDBOX/expect_ok" && stderr_empty; }

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
	'get workst/mid/secret'
	'get workst/d'
	'get workst/nonexistent'
	"get $LONG256"
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
[ "$refusals" -eq 23 ] || st=1
[ "$bad_err" -eq 0 ] || st=1
ok "$st" "5 stderr on refusal is exactly \"refused\" ($refusals refusals, $bad_err mismatched)"

# --- 6. ROOT itself is a symlink -> refused --------------------------------
EXTRA_ENV=(FLEET_OUTBOX_ROOT="$SANDBOX/rootlink" HOME="$FAKEHOME")
run_env 'get workst/d/tok'
st=0; is_refused || st=1
ok "$st" "6 ROOT itself a symlink refused"
EXTRA_ENV=()

# --- 7. symlinked directory in a middle segment -> refused -----------------
run_cmd 'get workst/mid/secret'
st=0; is_refused || st=1
ok "$st" "7 symlinked dir in middle segment refused (workst/mid -> out)"

# --- 8. relpath of 256 characters -> refused -------------------------------
st=0
[ "${#LONG256}" -eq 256 ] || st=1     # fixture really is 256 chars
run_cmd "get $LONG256"
is_refused || st=1
ok "$st" "8 relpath of 256 chars refused (limit is 255)"

# --- 9. HOME unset and FLEET_OUTBOX_ROOT unset -> refused, exact stderr ----
EXTRA_OPT=(-u HOME -u FLEET_OUTBOX_ROOT)
run_env 'get workst/d/tok'
st=0; is_refused || st=1
ok "$st" "9 HOME and FLEET_OUTBOX_ROOT both unset refused, stderr exactly \"refused\""
EXTRA_OPT=()

# --- 10. hostile PATH: a fake \`cat\` that prints PWNED is never used -------
FAKEBIN="$SANDBOX/fakebin"
mkdir -p "$FAKEBIN"
printf '#!/bin/sh\necho PWNED\n' > "$FAKEBIN/cat"
chmod +x "$FAKEBIN/cat"
st=0
fake=$(PATH="$FAKEBIN" cat 2>&1) || st=1
[ "$fake" = PWNED ] || st=1          # fixture sanity: fake cat really fires
ok "$st" "10 setup fake cat on PATH really prints PWNED"

for hostile in "$FAKEBIN" "$FAKEBIN:/usr/bin:/bin"; do
	EXTRA_ENV=(PATH="$hostile" FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME")
	run_env 'get workst/d/tok'
	st=0; is_served || st=1
	grep -q PWNED "$SO" && st=1
	ok "$st" "10 hostile PATH [$hostile] still serves real file bytes, no PWNED"
done
EXTRA_ENV=()

# --- 11. hostile BASH_ENV pointing at a file that prints PWNED -------------
# NOTE: bash sources $BASH_ENV during its OWN startup, before the script's
# first instruction, so no script body can stop it - only a bash startup
# decision can.  That is exactly why this reader is installed with the
# shebang "#!/bin/bash -p" (privileged mode: $BASH_ENV/$ENV are not
# processed), so case (b) below covers the "directly" invocation and case
# (a) covers the explicit `env BASH_ENV=... bash <script>` invocation in the
# same mode.  Case (c) additionally runs the plain `bash <script>` form and
# pins down what is left (bash's own pre-script sourcing, which the listed
# deployment precondition `sshd PermitUserEnvironment no` prevents).
BENV="$SANDBOX/pwnenv"
printf 'echo PWNED\n' > "$BENV"

# (a) via bash:  env BASH_ENV=... bash <script>
: > "$SO"; : > "$SE"
env BASH_ENV="$BENV" SSH_ORIGINAL_COMMAND='get workst/d/tok' \
	FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME" \
	bash -p "$READ" > "$SO" 2> "$SE"
rc=$?
st=0; is_served || st=1
grep -q PWNED "$SO" "$SE" && st=1
ok "$st" "11a env BASH_ENV=pwn bash <script>: PWNED not printed, real file bytes served"

# (b) executed directly (shebang is #!/bin/bash -p)
EXTRA_ENV=(BASH_ENV="$BENV" FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME")
run_env 'get workst/d/tok'
st=0; is_served || st=1
grep -q PWNED "$SO" "$SE" && st=1
ok "$st" "11b env BASH_ENV=pwn <script> direct: PWNED not printed, real file bytes served"
EXTRA_ENV=()

# (c) plain `bash <script>`: bash startup sources BASH_ENV before our first
#     instruction; assert the reader itself still adds nothing but the bytes.
: > "$SO"; : > "$SE"
env BASH_ENV="$BENV" SSH_ORIGINAL_COMMAND='get workst/d/tok' \
	FLEET_OUTBOX_ROOT="$ROOT" HOME="$FAKEHOME" \
	bash "$READ" > "$SO" 2> "$SE"
rc=$?
st=0
[ "$rc" -eq 0 ] || st=1
stderr_empty || st=1
tail -c 7 -- "$SO" | cmp -s -- - "$SANDBOX/expect_ok" || st=1
grep -q PWNED "$SE" && st=1
ok "$st" "11c plain env BASH_ENV=pwn bash <script>: reader's own output is exactly the file bytes"

if [ "$fails" -ne 0 ]; then
	echo "FAIL count: $fails"
	exit 1
fi
echo "ALL PASS"
exit 0
