The pre-push policy step no longer compiles pg-policy inside its 60 s check bound.
tools/preflight.py builds pg-policy once with the shared capped cargo target (bound:
the 600 s cargo bound), copies the binary privately and hands it to
tools/check_new_code_lang.py as PG_POLICY_BIN; without it the check still falls back
to cargo run. A failed build fails the policy step by name.
