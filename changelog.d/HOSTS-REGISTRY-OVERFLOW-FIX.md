The generated host registry lists `plangraph` as a public repository when the
registry has no `public_repos` key (an explicit list still wins), and carries a
per-host `remote_placement` switch that defaults to off: a registry-derived host
refuses remote work with "remote placement closed" until its entry sets it to
`true`. The doctor's SSH source probe gives WSL and Windows hosts a longer budget,
matching `plangraph_agent_ps`.
