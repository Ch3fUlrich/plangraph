"""Entry point; run from an installed checkout or with PYTHONPATH=src."""
from plangraph.keeper.crit_path_stall import main

if __name__ == "__main__":
    raise SystemExit(main())
