Measured host-local credit/usage limits, unavailable models and unserved legs now
cool down for one hour using existing run evidence. Routes retain cooled legs
after working legs; paid pins name only working free legs and need no override
when none remain. Missing/corrupt evidence is ignored.
# Round 2 hardening

Cooldown reads only recent-mtime records and explicit structured failed/skipped attempt codes, ignoring corrupt/deep JSON and all task prose. Paid pins reuse canonical routing filters and benchmark requirements and normalize reasons consistently. Tests exercise both live harness dispatches, host persistence, and 3000 stale records with only recent records opened.
