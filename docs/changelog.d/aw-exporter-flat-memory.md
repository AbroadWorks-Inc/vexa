- **Exporter: memory stays flat however long the meeting.** The named activity frames the speaker
  timeline is built from are no longer all held in memory: past 50,000 they wait on disk in sorted
  runs and are merged back in time order (the same order as before, ties included), and a
  speaker's level is read from the wav in blocks of at most 2 MiB instead of a whole window at
  once. Measured on synthetic 128 MB files, peak RSS of the activity pass: named frames 366 → 50
  MiB, mixed named/unnamed 231 → 51 MiB, tap frames 38.5 → 38.6 MiB; the level of a 1 h
  single-speaker wav 148 → 48 MiB. Every output is byte-identical.
