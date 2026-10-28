- **Exporter: one pod kill no longer quarantines the exports that happened to run beside the one
  that caused it.** A kill charges a crash to every export of the pod; a meeting with a crash now
  runs alone (it waits for the others to finish, and none starts while it runs), so its second
  crash is its own. The crash budget is checked even when no lease is left, a lease left by
  bookkeeping that failed after the job is released and never counted as a crash, and a stopped
  worker waits for its lease renewals before it releases the leases, so a renewal can no longer
  re-take one after a deploy. Queue items change only by conditional S3 writes, so a webhook, a
  rerun and the worker no longer overwrite each other, and the worker's S3 calls run off the
  event loop, so the webhook intake and `/healthz` stay responsive. A
  write that botocore resent after a lost answer is recognised (each write carries an id) and not
  applied twice. Bookkeeping that keeps failing after a job backs off and stops after
  `EXPORT_MAX_ATTEMPTS` failures until restart, instead of rerunning the job every sweep. ffmpeg
  runs have timeouts sized from their input, and a job past `EXPORT_JOB_TIMEOUT_SECONDS` (default
  4 h) fails `/healthz`, so the pod restarts and the hung meeting is charged a crash. On SIGTERM
  running exports get `EXPORT_STOP_WAIT_SECONDS` (100 s, grace period 120 s) to finish and record
  their outcome.
