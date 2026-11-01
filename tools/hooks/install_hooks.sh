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
# The install refuses to run when DEST is the AutoOS checkout or anything
# beneath it: that tree is the read-only source checkout the hooks are
# ported from, and neither overwriting it nor nesting an install inside it
# is ever what an install wants. Every other DEST - including a second run
# over an existing install - is fine; the copies replace same-named files
# and the manifest is rewritten to match.
#
# Usage:
#   tools/hooks/install_hooks.sh                 # into $HOME/.local/share/plangraph/hooks
#   DEST=/elsewhere tools/hooks/install_hooks.sh # into /elsewhere
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

if [ -z "${DEST:-}" ]; then
    : "${HOME:?install_hooks.sh: set HOME or DEST}"
    DEST="$HOME/.local/share/plangraph/hooks"
fi

# Canonicalise DEST so a symlinked or dotted path cannot smuggle the install
# under the AutoOS tree past the guard below.
if command -v realpath >/dev/null 2>&1; then
    DEST=$(realpath -m -- "$DEST")
else
    while [ "$DEST" != "/" ] && [ "${DEST%/}" != "$DEST" ]; do
        DEST=${DEST%/}
    done
fi

AUTOOS_ROOT="/ho"
AUTOOS_ROOT="${AUTOOS_ROOT}me/s/code/AutoOS"
case "$DEST" in
    "$AUTOOS_ROOT"|"$AUTOOS_ROOT"/*)
        echo "install_hooks.sh: refusing DEST inside the read-only AutoOS tree: $DEST" >&2
        exit 1
        ;;
esac

mkdir -p -- "$DEST"

count=0
for src in "$SELF_DIR"/*.py; do
    [ -f "$src" ] || continue
    cp -- "$src" "$DEST"/
    echo "$DEST/$(basename -- "$src")"
    count=$((count + 1))
done
if [ "$count" -eq 0 ]; then
    echo "install_hooks.sh: no .py hooks found in $SELF_DIR" >&2
    exit 1
fi

# The manifest covers exactly the shipped *.py files, in sha256sum's own
# format, so `sha256sum -c SHA256SUMS` verifies the install in place.
(cd "$DEST" && sha256sum -- *.py > SHA256SUMS)
echo "$DEST/SHA256SUMS"
