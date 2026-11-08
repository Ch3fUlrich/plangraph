Use the CI-exported TEMPORAL_CLI as a fallback for PG_TEMPORAL_CLI in the
worker crash acceptance test, still failing when neither is set. Move the
unchanged replay base64 decoder into test support to keep replay.rs below
its existing file-size baseline without removing assertions.
