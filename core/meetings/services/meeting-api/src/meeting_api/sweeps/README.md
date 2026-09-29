# meeting_api.sweeps — single-flight guard for background sweeps (#637)

At `meetingApi.replicaCount > 1` every meeting-api replica starts the same background loops, so each
sweep's real work runs once **per replica** instead of once per interval. This package makes that
safety structural rather than accidental.

`single_flight` wraps a sweep tick in a Postgres **session-level advisory lock** keyed by loop name
(a fixed `classid` disjoint from the per-user `pg_advisory_xact_lock` keyspace): the replica that
acquires the lock runs the tick, the others skip it that interval. A replica that dies mid-tick drops
its session lock on disconnect, so the next interval is picked up elsewhere — no leader-election infra.

The guard **degrades to run-the-tick** when no DB session factory is available (single-replica / Lite),
so single-replica behaviour is unchanged. The `segment-consumer` loop is deliberately **not** wrapped —
it is already single-delivery via the Redis consumer group, and wrapping it would serialize the
replicas' stream reads.

`item_failures` bounds the sweeps' work (§6.9 F-I). `run_pages` is the one paged loop: it reads a
sweep's work `SWEEP_BATCH_SIZE` items at a time in a stable order, skips the items the sweep has
given up, and runs every other one through `run_item`, until a short page. `run_item` logs a
failing item with its id and stack, counts it in `aw_sweep_items_total{sweep,result}`, and after
`SWEEP_MAX_ITEM_FAILURES` (kept per sweep in `sweep_item_failures`, so every replica shares the
count) gives it up for good; it never raises. The sweeps that use them are `auto-join`, `not-sent`,
`webhook-publisher` (its own page loop, with `run_item` per row when a page's transaction fails),
and the reconcile sweep's `unproven-teardown` and `retry-overdue`. Each takes the give-up record
(`ItemFailures`) as a required argument: the composition root passes `PostgresItemFailures`, and
without Postgres (Lite) one `InMemoryItemFailures` for the process.
