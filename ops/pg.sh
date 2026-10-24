#!/bin/sh
# pg wrapper for build-L1; token via PGTOK env (default plangraph-L1-main)
export PG_URL=http://127.0.0.1:8787
export PYTHONPATH=/home/s/code/plangraph-worktrees/build-L1-bus/src
export PG_TOKEN_FILE=$HOME/.config/plangraph/${PGTOK:-build-L1}.token
exec python3 -m plangraph.keeper.cli "$@"
