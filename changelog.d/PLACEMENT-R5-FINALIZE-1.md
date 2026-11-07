Added receiver-side RESULT quarantine import with hash, bundle, ancestry, Writer trailer, and reachable-history secret verification.

Receiver finalization now publishes quarantine refs only after all checks pass, rejects tag heads, and tests refusal isolation with hermetic fixtures.
