Keeper claim preflight now uses a configurable 30-second budget and reports the
step that exceeded it. Claims waiting on an in-flight lookup use the leader's
remaining budget, bounded by their own deadline.
