Fix Linux spawner containment with a private per-run subreaper anchor, keeping setsid descendants attributable after their command exits and waiting on adopted children. Live and recorded cancellation verify descendants; unmocked cancellation and probe cleanup regressions cover orphaned descendants without cgroup delegation.
Fix round 1: bound adopted-child cleanup to a ten-second grace, forward wrapper
termination signals, catch late forks during verified tree kills, and distinguish
containment denial from harness exit 125. If the wrapper is SIGKILLed, the spawner
uses its pre-kill identity snapshot (including setsid descendants), plus surviving
group/session membership; the anchor normally stays alive to reap adopted children.
Fix round 2: fail closed on unreaped wrapper children, forward HUP/QUIT without
signalling reaped groups, validate containment markers after terminal events,
recheck fallback identities, and reject surviving recorded setsid members.
Fix round 3: the wrapper drains os.waitpid before reporting success so a live
unreaped adopted child never yields exit 0, an alive group without a listable
member fails closed as KillIncomplete, and without a pidfd nothing is signalled -
the survivor stays recorded and is reported instead of risking a reused pid.
