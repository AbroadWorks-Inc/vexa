# AW Bots handbook

Current behavior of AbroadWorks's Vexa fork, as the code on `development` implements it. The fork
[README](../../README.md) is the front door: the picture of the system, the `/v2` route map, the
image list, the configuration table, and how to build. This directory is the rest.

Client field lists and `curl` examples stay in the
[`/v2` API reference](../../core/meetings/services/meeting-api/V2-API.md).
Install steps stay in aw-notetaker `deployment/base/aw-bots/README.md`.

| Read this | When you need |
|---|---|
| [architecture.md](architecture.md) | Which service does what, and who is allowed to call whom |
| [meeting-lifecycle.md](meeting-lifecycle.md) | Entries, meetings, when a bot is sent, and when it is replaced |
| [recording-and-speakers.md](recording-and-speakers.md) | Audio, `speaker-activity.jsonl`, per-speaker channels, Meet / Zoom / Teams / Jitsi, the runtime pod |
| [export-and-webhooks.md](export-and-webhooks.md) | Status events, delivery, and the handoff to notetaker-worker |
| [security.md](security.md) | Signed identity, callback checks, webhook secrets, erasure |
| [data-and-configuration.md](data-and-configuration.md) | Tables we added, and the settings that are easy to misread |
| [observability.md](observability.md) | Metrics and the alert rules |
| [decisions/](decisions/README.md) | Why the system is shaped this way |
| [archive/](archive/README.md) | The dated designs and plans. History, not the spec |

The exporter package README
([`integrations/out/aw-notetaker/README.md`](../../integrations/out/aw-notetaker/README.md))
is the adapter's own page: its environment, its HTTP replies, and how it queues work.
