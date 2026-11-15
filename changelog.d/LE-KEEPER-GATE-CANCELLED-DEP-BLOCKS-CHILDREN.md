Cancel now drops incoming dependencies from live gates and goals atomically,
with an event recording each drop and active review epochs invalidated; an edge
that is the only path from live work to a goal is kept so the cancel stays legal.
Define reports stale cancelled-gate dependency refusals per child while creating
independent items, naming the blocked gate, dependency, and remove_dep recovery.
Keeper check lists stale cancelled gate dependencies in text and JSON.
Frozen gates retain their existing history and are reported for operator repair.
