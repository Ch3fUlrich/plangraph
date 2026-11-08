Fixed: Only writer-role runs arm the shared git configuration guard.
Review and research runs no longer restore or fail on unrelated git configuration changes.
Other roles, including unknown or malformed roles, fail closed and remain guarded.
Restart enforcement uses the persisted guard flag and restores post-capture mutations only for guarded runs.
