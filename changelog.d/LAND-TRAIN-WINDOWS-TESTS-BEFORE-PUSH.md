`merge_batch push` runs the CI Windows test selection against the batch head on the native Windows host
(`PLANGRAPH_WINDOWS_HOST`) in a disposable checkout before pushing; a red run refuses the push with the
failing test ids and a green one is recorded in the GO-REQ with its duration.
