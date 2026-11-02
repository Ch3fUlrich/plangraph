#!/bin/sh
# install_hooks.sh: install the plangraph hook scripts into a user's tree.
#
# Copies every `tools/hooks/*.py` - the hook entry points and the rule
# modules they import - from this script's own directory into DEST
# (default: ${DEST:-$HOME/.local/share/plangraph/hooks}), writes a
# DEST/SHA256SUMS manifest over the copied files via sha256sum, and prints
# the list of installed files (one destination path per line, SHA256SUMS
# last). Only *.py is shipped: the tests/ tree and non-Python files such as
# the pre-push hook are never copied here.
#
# The install refuses to run when DEST is the read-only source checkout this
# script ships from - the AutoOS tree the hooks are ported from when run from
# that tree - or anything beneath it: neither overwriting that tree nor
# nesting an install inside it is ever what an install wants. Every other
# DEST - including a second run over an existing install - is fine; the
# copies replace same-named files and the manifest is rewritten to match.
#
# Usage:
#   tools/hooks/install_hooks.sh                 # into $HOME/.local/share/plangraph/hooks
#   DEST=/elsewhere tools/hooks/install_hooks.sh # into /elsewhere
set -eu

# shellcheck disable=SC1007  # `CDPATH= cd` is a deliberate empty-value assignment prefix
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

if [ -z "${DEST:-}" ]; then
    : "${HOME:?install_hooks.sh: set HOME or DEST}"
    DEST="$HOME/.local/share/plangraph/hooks"
fi

# Lexical canonicalisation for hosts without realpath: absolute, no duplicate
# slashes, no trailing slash, `.` segments dropped, `..` resolved against the
# previous segment. Symlinks are not resolved here.
canon_path() {
    _cp_rest=$1
    case $_cp_rest in
        /*) ;;
        *) _cp_rest=${PWD%/}/$_cp_rest ;;
    esac
    _cp_out=
    while [ -n "$_cp_rest" ]; do
        case $_cp_rest in
            */*)
                _cp_seg=${_cp_rest%%/*}
                _cp_rest=${_cp_rest#*/}
                ;;
            *)
                _cp_seg=$_cp_rest
                _cp_rest=
                ;;
        esac
        case $_cp_seg in
            ''|.) ;;
            ..) _cp_out=${_cp_out%/*} ;;
            *) _cp_out=$_cp_out/$_cp_seg ;;
        esac
    done
    [ -n "$_cp_out" ] || _cp_out=/
    printf '%s\n' "$_cp_out"
}

# Canonicalise DEST so a dotted path cannot smuggle the install under the
# source checkout past the guard below. realpath also resolves symlinks; the
# lexical fallback cannot - only realpath resolves symlinks - so with
# realpath absent a symlinked DEST can still point into the checkout
# undetected.
if command -v realpath >/dev/null 2>&1; then
    DEST=$(realpath -m -- "$DEST")
else
    DEST=$(canon_path "$DEST")
fi

# AUTOOS_ROOT is the read-only source checkout this script ships from: the
# root of the checkout that holds tools/hooks - the AutoOS tree the hooks are
# ported from when run from that tree. It is derived from this script's own
# location so the guard below is live on every host and every clone, not dead
# code against one machine's hard-coded path. The fallback is one unsplit
# literal (the historical AutoOS location, per user): a constant assembled
# from string fragments is a scanner-evasion pattern, not a path.
# shellcheck disable=SC1007  # `CDPATH= cd` is a deliberate empty-value assignment prefix
AUTOOS_ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd -P) ||
    AUTOOS_ROOT="${HOME:-}/code/AutoOS"
case "$DEST" in
    "$AUTOOS_ROOT"|"$AUTOOS_ROOT"/*)
        echo "install_hooks.sh: refusing DEST inside the read-only source checkout: $DEST" >&2
        exit 1
        ;;
esac

mkdir -p -- "$DEST"

count=0
set --
for src in "$SELF_DIR"/*.py; do
    [ -f "$src" ] || continue
    cp -- "$src" "$DEST"/
    echo "$DEST/$(basename -- "$src")"
    set -- "$@" "$(basename -- "$src")"
    count=$((count + 1))
done
if [ "$count" -eq 0 ]; then
    echo "install_hooks.sh: no .py hooks found in $SELF_DIR" >&2
    exit 1
fi

# The manifest covers exactly the files this run shipped - never a stale file
# left over in DEST - in sha256sum's own format, so `sha256sum -c SHA256SUMS`
# verifies the install in place.
(cd "$DEST" && sha256sum -- "$@" > SHA256SUMS)
echo "$DEST/SHA256SUMS"
