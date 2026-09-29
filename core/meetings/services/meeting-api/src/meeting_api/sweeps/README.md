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
count) gives it up (`given_up`); it never raises. An item's action can also raise:

- `ItemExpired`: the item is past its sweep's age bound and is given up at once
  (`ItemFailures.give_up`);
- `ItemDeferred(result, why)`: it got no answer for a reason that isn't its failure (a runtime
  that didn't answer, `result="runtime_unreachable"`). It is logged at warning level and counted
  under that `result`, nothing is recorded, and it runs again next pass.

The teardown and reconcile sweeps decide between these through one rule,
`lifecycle.reconcile.runtime_bound`. Past `UNPROVEN_TEARDOWN_MAX_AGE_S` since the item's `since` (a
pending teardown's record time, a reconcile row's `updated_at`) it is expired. A runtime that
doesn't answer, with a known `since`, is deferred. A definite refusal, or an unreachable runtime with
no readable `since`, is one of the item's tries. A sweep whose given-up item must still end passes
`on_given_up`, run once right after the give-up: the auto-join tick ends an open-ended meeting it
gave up `not_sent`.

The sweeps that use them, with each read's item ids:

- `auto-join` (`due:<id>`, `retry:<id>`);
- `not-sent`;
- `webhook-publisher` (its own page loop, with `run_item` per row when a page's transaction fails);
- the reconcile sweep's `unproven-teardown` (`<meeting id>:<workload>`) and `retry-overdue`
  (`waiting:<id>`, `unfinished-spawn:<id>`);
- upstream's reconcile loops `stale-stopping` and `stale-nonterminal`, a page by meeting id.

Each takes the give-up record (`ItemFailures`) as a required argument: the composition root passes
`PostgresItemFailures`, and without Postgres (Lite) one `InMemoryItemFailures` for the process.

`given_up`, the read every sweep makes of the items it lists, touches each record it finds given
up, so a given-up record is kept while its item is still listed. `prune_item_failures`, run on
every pass of the reconcile sweep, deletes the records untouched for
`SWEEP_ITEM_FAILURES_RETENTION_S` (7 days), `SWEEP_BATCH_SIZE` at a time. What it removes is the
given-up items no sweep lists any more (their meeting gone or finished, their work cleared) and
the records of items that stopped failing.
