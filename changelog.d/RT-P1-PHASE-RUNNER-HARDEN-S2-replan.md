Replan: pin phase branch FNV-1a-64 independently of content hashing, consolidate phase-finish test support, exercise real-repository rejection, and remove workflow/replay whitespace churn.

Unresolved: legacy finish authorization still lacks pre-patch provenance. The existing keeper request supplies only key, branch, and head; neither a checked-out ref nor a caller-supplied boolean proves a Temporal patch marker. Closing this safely needs an authenticated workflow-history/provenance contract, including compatibility for recorded activity payloads. Legacy success assertions remain unchanged; no new rejection claim is made for a checked-out matching legacy ref.

Validation: 97 Python tests pass with PYTHONPATH=src:tests and importlib mode; two Rust mirrored-vector tests and 18 replay tests pass. Mutating the pinned FNV prime makes both Rust vector tests fail. Existing vectors already passed before extraction; this is contract isolation, not a claim that the previous hash produced different bytes.

Size compliance remains unmet: check_file_size reports phase.rs (635 vs baseline 631), phase_workflow.rs (411 vs 406), and replay.rs (416 vs 413). The PR also already includes a 2878-line E2E suite. No allow entry or baseline was changed; splitting all these suites is not represented as completed.
