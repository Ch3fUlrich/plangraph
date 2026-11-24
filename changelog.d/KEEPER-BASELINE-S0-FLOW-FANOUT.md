Added the PostgreSQL keeper baseline reports for current flow and transitive fanout.
Both reports use one read-only, repeatable-read snapshot. The flow report lists the
two ready modes and priority extensions. The fanout report includes idle age and
counts unknown timestamps. The event count window does not filter the graph.
