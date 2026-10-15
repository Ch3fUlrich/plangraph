#!/bin/sh
# probe.sh LABEL - health, verdict bool probe on a nonexistent key, router whoami
U=http://127.0.0.1:8787
echo "[$1] health: $(curl -s -m 5 $U/v1/health)"
RT=$(cat $HOME/.config/plangraph/reviewer-openai.token)
echo "[$1] verdict ok:\"false\": $(curl -s -m 10 -o /dev/stderr -w '%{http_code}' -X POST -H "Authorization: Bearer $RT" -H 'Content-Type: application/json' -d '{"key":"plangraph:NO-SUCH-GO-CHECK","ok":"false","reason":"go check D-1331"}' $U/v1/verdict 2>&1)"
TT=$(cat $HOME/.config/plangraph/router.token)
echo "[$1] router whoami: $(curl -s -m 5 -H "Authorization: Bearer $TT" $U/v1/whoami)"
