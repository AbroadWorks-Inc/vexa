- **Exporter: a meeting that kills the exporter pod no longer loops forever.** A job is leased
  before it starts and the lease is renewed while it runs; a lease found expired means the run was
  killed (eviction, out of memory), which raises nothing. That counts one crash, and after
  `EXPORT_MAX_CRASHES` (default 2) the meeting is quarantined like one out of attempts and never runs
  again on its own. A stopped worker releases its leases, so a deploy is not a crash. The worker is
  now a continuous pool: one long export no longer holds back the meetings behind it.
