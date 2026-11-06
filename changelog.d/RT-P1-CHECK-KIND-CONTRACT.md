Normalize legacy empty claim check kinds to `change` in Python, matching Rust.
Keep unknown recorded Rust claim kinds readable while rejecting execution as a
contract defect. Cover metadata parity, legacy decoding, and raw-empty red-first
execution without relaxing the unknown-kind validation contract.
