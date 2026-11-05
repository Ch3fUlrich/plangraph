### Fixed
- Missing delivery bridges warn in status without failing ticks; configured bridge
  failures still fail ticks. Flush restart-pending findings through SSH outages
  with stable event IDs, retaining failed sends and unmeasured delivery history.
- Preserve delivery-window entries after failed notification and across restart
  while remote pending findings are unmeasured; retry durable notifications.
- Create the watchdog health directory explicitly before writing fresh-state health.
- Report missing delivery configuration for all findings and pending outbox records;
  merge partial health records and require service-file probe configuration.
- Retain original test names with updated behavior assertions and add regressions
  for fresh-state health writes and restart eviction protection.
