- **Exporter: a long meeting with many speakers no longer fills the exporter's disk.** Each
  per-speaker channel is padded onto the meeting clock (about 230 MB per channel for a 2-hour
  meeting), and every channel was built locally before any was uploaded: meeting 195 (19 channels)
  exceeded the pod's 8 GiB and Kubernetes evicted the exporter on every retry. Channels are now
  fetched, joined, uploaded and deleted one at a time; identities are still checked before any
  audio is fetched, and `index.json` is still written last.
