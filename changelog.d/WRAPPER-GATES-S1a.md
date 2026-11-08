### Writer file-size gate

Writer result finalization now enforces the file-size gate in Rust for every
caller, including export revalidation. Violations HOLD the result with the gate
and offending path named, before bundle export or publication. The existing
Python CI gate remains unchanged; shared fixture tests check agreement.
