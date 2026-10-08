# Source note for batch 2026-10-03-router-progress-d492-d524

Router order (routing 2026-10-03, operator): update the graph with today's progress from the decision rows D-492..D-524 in
/home/s/code/routing/docs/decisions-log.md.

- Closes tokens not yet applied: D-516 (server:IDS-READSHAPE@9be5bb40, server:UPR-LOG-B1@741a5fd6) and D-519
  (server:STREAMING-BACKUP@f16d09bb, server:VIDEO-CAP@a0730a30, server:HA-FLOORPLAN-REAL@20e47d08, server:GW-SEMAPHORE-HARDEN@12af549b).
  D-502..D-504 are already live (2026-10-03-router-closes-d502-d504). None of the six shas is an ancestor of the local Server
  origin/main (6edbcd7a) at authoring time: items go in_review with the sha in the note, never done. Only server:HA-FLOORPLAN-REAL exists in
  the graph; the other five Server lanes come from server-L1's own plan batch and are NOT created here.
- New items: projects workst-ebn (D-514, D-515, D-516, D-518, D-522, D-523) and workst-gen (D-512, D-514, D-519, D-520, D-521, D-524);
  AutoOS lanes COMBO-REFRESH (D-510, D-520), WIN-PROMPT (D-524), FAMILY-TRACK (routing 2026-10-03), G-KEEPER (D-512),
  OpenCode fleet O1-O6 + LEAN-MCP (D-517, D-518), workstation L0 failover (D-513), runner scale (D-512), launcher CPU gate (D-523).
- Definitions of U12, LEAN-MCP, ROUTE-FRESH are taken only from the router's message wording: marked "per router, to refine".
