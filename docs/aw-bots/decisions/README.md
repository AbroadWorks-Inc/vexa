# Decisions

Short records of choices that are still in force. The long argument is in the
[archive](../archive/README.md).

| | Decision |
|---|---|
| [0001](0001-vexa-as-the-meeting-platform.md) | Vexa is the meeting platform. The existing notetaker still writes the transcript. |
| [0002](0002-speaker-activity-file.md) | Every meeting writes `speaker-activity.jsonl`, with no audio in that file. |
| [0003](0003-exporter-hands-off-to-notetaker.md) | A separate exporter builds the notetaker folder and calls `/process`. |
| [0004](0004-one-meeting-per-link.md) | One account, one link, overlapping times: one meeting and one live bot. |
| [0005](0005-webhooks-replace-the-system-hook.md) | Status leaves through `/v2/webhooks` subscriptions. |
| [0006](0006-signed-gateway-identity.md) | meeting-api and admin-api trust `x-user-id` only with the gateway's signature. |
| [0007](0007-replace-a-failed-bot-on-the-same-meeting.md) | A bot that fails while the meeting is on is replaced on that same meeting. |
| [0008](0008-per-speaker-channels.md) | Meet and Jitsi record each remote channel beside the mix. |
| [0009](0009-jitsi-records-through-aw-bots.md) | Jitsi records through AW Bots; Jibri and live captions are gone. |
| [0010](0010-per-person-transcript-is-delivered.md) | The per-person transcript is the one delivered (`deliver`). |
| [0011](0011-transcriber-pulls-from-a-queue.md) | The transcriber takes work from a Redis queue, up to 3 GPUs, and owns the GPU-time limit. |
| [0012](0012-lobby-wait-ten-minutes.md) | A bot waits 10 minutes in a lobby, on every platform. |
| [0013](0013-export-folders-name-the-meeting.md) | Export folders carry the platform, the native meeting id and the start. |
| [0014](0014-master-built-on-read.md) | The master recording is built when it is read. |
| [0015](0015-jitsi-channels-have-no-cap.md) | Per-speaker Jitsi has no channel cap; the bot is optimised instead. |
| [0016](0016-zoom-hints-need-audio.md) | A Zoom speaker hint is sent only while remote audio arrives. |
