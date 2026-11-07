Isolate every Dell CI job with bwrap and private checkouts, caches and environment; keep Docker in the trusted driver (D-4670).
Support local Dell execution, retain bounded scrubbed job diagnostics, and fix private checkout path expansion and bundle imports.
Mark Dell pytest runs explicitly so unavailable real bwrap probes fail closed there and skip with a named reason elsewhere (D-4755).
