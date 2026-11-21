Pytest prioritizes the checkout under test, and concurrent worktree tests exercise the lock during path allocation as well as Git creation.
The import regression places the absolute source path after a competing checkout in PYTHONPATH.
