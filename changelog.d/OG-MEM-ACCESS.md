- Add an opt-in `--memory-probe` to host doctor. It uses the Rust Omnigraph
  client to check endpoint health and authenticate a read of the plangraph
  project hub. The doctor reports fixed status messages and leaves credential
  values and server response bodies out of its output.
