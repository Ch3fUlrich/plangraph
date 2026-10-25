#!/bin/bash
# heartbeat all build-L1-bus claims every 8 min; exit after N cycles to wake L1
cd /home/s/code/plangraph-worktrees/build-L1-bus
N=${1:-3}
for i in $(seq 1 $N); do
  while read -r k; do [ -n "$k" ] && echo "$(date -u +%H:%MZ) $k: $(sh ops/pg.sh heartbeat "$k" 2>&1 | tail -1)"; done < ops/hb-keys.txt
  [ $i -lt $N ] && sleep 480
done
exit 0
