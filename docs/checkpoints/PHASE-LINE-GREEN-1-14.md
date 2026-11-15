# PHASE-LINE-GREEN-1 checkpoint 14

Checkpoint 14 covers remaining policy and sensitivity contracts. The sensitivity sweep
uses synthetic leg state and checks all five stability seeds independently; run
`python3 -m pytest -q -p no:cacheprovider tests/test_spawner_docs_contract.py`
to verify reproducibility and CLI serialization.
