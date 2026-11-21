Fixed the D-6517 findings of the OmniRoute leg state: OpenCode model discovery no longer
inherits the parent-only management token variables, a model that is not listed returns
`not_listed` before any model-run probe, and the default hung-leg sampler and cancel read the
persisted `pid_start` identity.
