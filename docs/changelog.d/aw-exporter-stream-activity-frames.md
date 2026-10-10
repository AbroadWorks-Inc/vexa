- **Exporter: activity frames are written to disk as they are read.** `speaker_activity_frames.json`
  was built from every parsed frame held in memory, then serialised again; at the 128 MB activity
  cap that took ~1 GiB per export (pod limit 1.5 GiB, 4 exports at once). Frames now stream to a
  file and the JSON is assembled from it: measured on the frame path, a 128 MB tap-frame file 1,032 →
  37 MiB, a 128 MB named-frame file 1,167 → 381 MiB (the named frames the timeline needs stay in
  memory), meeting 195's real file 66 → 52 MiB with the same 30,737 frames.
