# BUS-PEER-DISCOVERY-ADOPT evidence (post when the claim is allowed, i.e. after RT-P1-BUS-DISCOVERY is done)
- AutoOS main 2629d740 = no-ff merge of lane 922025f2 (D-2730 GO). prepush green, 3 checks, for 2629d7404ca4.
- AutoOS seats: qwen3.8-flash APPROVE, /home/s/code/AutoOS/logs/agents/20261025-090814-discovery-adopt-autoos-s-37699a/output.log (sha256 0871d89bbe95ce8f…); haiku APPROVE, …/20261025-090818-discovery-adopt-autoos-s-16b106/output.log (f3589408e5aa3486…). Writer mimo/mimo-v2.6-pro (xiaomi).
- routing lane 46b7d6a1: HELD (D-2730) until the phase is on main and the keeper is deployed; routing sets FLEET_BUS_HOST/FLEET_KEEPER_URL/FLEET_KEEPER_TOKEN_FILE, then merges. Seats: qwen APPROVE …/20261025-083740-discovery-adopt-routing-10eeda/output.log (ec15d1e7308c46fd…); haiku ACCEPT …/20261025-083744-discovery-adopt-routing-a876c0/output.log (941f261b0256ae34…). pytest 190 passed.
- IP-literal criterion (D-2730 (2)): `git grep` over 100.64/10 finds no hits in code, bin, ops or tests in either repo. The append-only routing history docs are excluded.
- Do NOT ff any shared live AutoOS checkout (code host or workst) until keeper /v1/hosts is deployed (D-2730).
