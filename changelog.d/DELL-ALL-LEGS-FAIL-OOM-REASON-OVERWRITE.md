Strip the OOM wrapper's plangraph_oom telemetry line from captured stderr; it is recorded only as an event, so failed attempts keep their real reasons.
