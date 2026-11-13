#!/bin/sh
# install_hooks.sh: install the plangraph hook scripts into a user's tree.
#
# Copies every `tools/hooks/*.py` - the hook entry points and the modules they
# import - from this script's own directory into DEST (default:
# ${DEST:-$HOME/.local/share/plangraph/hooks}), writes a DEST/SHA256SUMS
# manifest over the copied files via sha256sum, and prints the list of
# installed files (one destination path per line, SHA256SUMS last). Only *.py
# is shipped: the tests/ tree and non-Python files such as the pre-push hook
# are never copied here. A second run over an existing install is fine: the
# copies replace same-named files and the manifest is rewritten to match.
#
# The install refuses (exit 1, nothing created) a DEST that is, or lies
# beneath, either tree it must never write into: the checkout this script
# ships from, or the read-only AutoOS tree the hooks were ported from
# ($AUTOOS_ROOT, else $HOME/code/AutoOS). DEST is canonicalised first
# (symlinks resolved where realpath exists), so a dotted or symlinked path
# cannot get past the check.
#
# This script only copies files: it registers nothing and never edits Claude
# Code settings. Register each hook entry point yourself, always through the
# guarded command form
#
#   sh -c 'test -f "$1" || exit 1; exec python3 "$1"' _ DEST/<hook>.py
#
# never as a bare `python3 DEST/<hook>.py`. A hook script that is missing
# (moved, half-installed, mistyped) must exit 1, never 2: python3 itself exits
# 2 when its script file does not exist, and exit 2 is the Claude Code block
# code, so a bare command registered on PreToolUse would deny every Bash call.
# The `test -f` gate owns the missing-file case (exit 1, silent); an existing
# script is exec'd untouched, so the wrapped run is the direct run.
#
# Entry points and their events (the other *.py files are modules they import):
#   bash_guard.py      PreToolUse (Bash)
#   context_guard.py   PostToolUse; ctx_guard_session.py is the same guard with
#                      per-session thresholds - register one of the two, not both
#   fleet_inject.py    PostToolUse and UserPromptSubmit
#   fleet_report.py    Stop
#   session_title.py   UserPromptSubmit
# Rust hook turn-budget-hook is built/installed separately; see
# docs/turn-budget-hook.md for events and the missing-binary guard. This
# Python-only copier does not ship it or register any hook.
#
# Usage:
#   tools/hooks/install_hooks.sh                 # into $HOME/.local/share/plangraph/hooks
#   DEST=/elsewhere tools/hooks/install_hooks.sh # into /elsewhere
#   tools/hooks/install_hooks.sh --help          # print this text
set -eu

# shellcheck disable=SC1007  # `CDPATH= cd` is a deliberate empty-value assignment prefix
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# The leading comment block above, without the shebang and the `# ` prefix.
usage() {
    while IFS= read -r _u_line; do
        case $_u_line in
            '#!'*) ;;
            '#') echo ;;
            '# '*) printf '%s\n' "${_u_line#"# "}" ;;
            *) break ;;
        esac
    done < "$0"
}

case ${1:-} in
    '') ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        echo "install_hooks.sh: unknown argument: $1 (see --help)" >&2
        exit 2
        ;;
esac

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

# Physical canonicalisation for hosts without realpath: walk the path one
# segment at a time, entering each existing directory with `cd -P` (symlinks
# resolved, `..` taken physically); the first segment that does not exist
# starts a tail that is appended lexically.
canon_phys() {
    _cf_rest=$1
    case $_cf_rest in
        /*) ;;
        *) _cf_rest=${PWD%/}/$_cf_rest ;;
    esac
    _cf_cur=/
    _cf_tail=
    while [ -n "$_cf_rest" ]; do
        case $_cf_rest in
            */*)
                _cf_seg=${_cf_rest%%/*}
                _cf_rest=${_cf_rest#*/}
                ;;
            *)
                _cf_seg=$_cf_rest
                _cf_rest=
                ;;
        esac
        case $_cf_seg in ''|.) continue ;; esac
        if [ -z "$_cf_tail" ] && [ -d "$_cf_cur/$_cf_seg" ]; then
            # shellcheck disable=SC1007
            if _cf_new=$(CDPATH= cd -P -- "$_cf_cur/$_cf_seg" 2>/dev/null && pwd -P); then
                _cf_cur=$_cf_new
            else
                _cf_tail=$_cf_tail/$_cf_seg
            fi
        else
            _cf_tail=$_cf_tail/$_cf_seg
        fi
    done
    if [ -n "$_cf_tail" ]; then _cf_tail=$(canon_path "$_cf_tail"); fi
    case $_cf_cur in
        /) printf '%s\n' "${_cf_tail:-/}" ;;
        *) printf '%s\n' "$_cf_cur${_cf_tail%/}" ;;
    esac
}

# realpath resolves symlinks; without it canon_phys does, so a symlinked path
# cannot point into a guarded tree undetected on either kind of host.
canon() {
    if command -v realpath >/dev/null 2>&1; then
        realpath -m -- "$1"
    else
        canon_phys "$1"
    fi
}

# Canonicalise DEST so a dotted or symlinked path cannot smuggle the install
# into a guarded tree past the checks below.
DEST=$(canon "$DEST")

# refuse_inside ROOT WHAT: exit 1 when DEST is ROOT or lies beneath it. ROOT is
# canonicalised like DEST; an empty ROOT (unset variable) guards nothing.
refuse_inside() {
    [ -n "$1" ] || return 0
    _ri_root=$(canon "$1")
    case $DEST in
        "$_ri_root"|"${_ri_root%/}"/*)
            echo "install_hooks.sh: refusing DEST inside $2 ($_ri_root): $DEST" >&2
            exit 1
            ;;
    esac
}

# The source checkout is derived from this script's own location, so the check
# is live on every host and every clone. The AutoOS tree is named the way the
# rest of this repository names it: $AUTOOS_ROOT, else $HOME/code/AutoOS (both
# are checked; neither is a machine-specific literal).
# shellcheck disable=SC1007  # `CDPATH= cd` is a deliberate empty-value assignment prefix
SRC_ROOT=$(CDPATH= cd -- "$SELF_DIR/../.." && pwd -P)
refuse_inside "$SRC_ROOT" "the source checkout"
refuse_inside "${AUTOOS_ROOT:-}" "the read-only AutoOS tree"
refuse_inside "${HOME:+$HOME/code/AutoOS}" "the read-only AutoOS tree"

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
