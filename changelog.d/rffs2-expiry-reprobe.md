### RFF-S2 expiry/reprobe (partial)

- Remove the implement-key lead-writer pin (D-5092); preserve the concrete
  free-before-subscription ordering regression test.
- Restore operator leg `expires` timestamps with injectable-clock eligibility
  and finite timestamp validation. Permanent safety disables remain disabled.
- Live local spawner probes: console-meta Muse passed; opencode-go Muse failed
  with exit 7. Both outcomes were recorded through the existing probe mechanism.
- Automatic reprobe integration and D-5113 registration remain outstanding.
