Writer fast checks now run public scrub rules and credential shapes together with
private words. The legacy private-scrub entry point delegates to scrub_check;
preflight uses one invocation. Tree, committed and hook checks share the same
content scanner, including path/value allowances and symlink refusal.
