# tests — unit tests

One `test_<module>.py` per module in `exporter/`; S3 is faked with moto and HTTP with
`httpx.MockTransport`, so nothing leaves the machine. `builders.py` makes synthetic
speaker-activity lines (no real meeting data), webhook envelopes, and `sweep_once`: one tick of
the worker (`run_worker`'s own `_run_item` for every pending meeting, awaited). Each call is a new
worker (a new lease owner) unless the same `WorkerState` is passed to several.

Run from the package folder: `pytest -q`. The Docker-based end-to-end test lives in
`integration/` and is skipped unless you pass `-m integration`.
