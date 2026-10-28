- **Exporter: one command exports a finished meeting again and redoes its transcript.**
  `python -m exporter.rerun <meeting-uuid> ...` in the exporter pod reads each meeting from aw-bots
  and queues it as a rerun: the export writes every file again, filling in any an earlier export
  could not write, and hands the folder to `notetaker-worker`'s `/process` with `"rerun": true`.
  Unfinished, never-sent and never-joined meetings are refused.
