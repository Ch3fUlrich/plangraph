#!/bin/sh
# seat.sh MODEL PROMPT OUTDIR TITLE - one tool-less gateway review call (D-1317)
cd /home/s/code/AutoOS || exit 2
exec timeout 1200 python3 tools/autoos_gateway_key.py exec -- python3 tools/review-call.py \
  --model "$1" --prompt-file "$2" --out-dir "$3" --title "$4"
