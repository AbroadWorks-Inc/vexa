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
