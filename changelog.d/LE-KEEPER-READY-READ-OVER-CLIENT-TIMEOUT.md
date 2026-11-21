`ready` no longer rebuilds the git work index (origin fetch, open-PR list, `git log --all`)
inside a request once one exists: the last built index is served and one background refresh
starts when it is older than 10 s, so the read stays well inside the client timeout. The JSON
ready read builds its groups once (it built them twice). `pg ready`/`blocked`/`waves` use the
120 s client timeout. With no client config the READ-ONLY fallback reads the daemon's default
snapshot (`~/.local/share/plangraph/plan.snapshot.json`), never the cwd, and a missing snapshot
is a named refusal instead of a traceback.
