#!/bin/sh
# run python in the keeper's env (DSN from env file, never printed)
set -a; . /home/s/.config/plangraph/pg-keeper.dsn; set +a
export PG_KEEPER_BACKEND=postgres PG_SCHEMA=keeper PG_CONFIG=/home/s/.local/share/plangraph/keeper.json
export PYTHONPATH=/home/s/code/plangraph/.claude/worktrees/live-2d61e281/src
cd /home/s/code/plangraph/.claude/worktrees/live-2d61e281
exec /home/s/.local/share/plangraph/venv-pg/bin/python "$@"
