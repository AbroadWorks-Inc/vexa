# LiveKit website meetings (phase 1): completion report

Date: 2026-11-14 · Branches `feat/livekit-bot` in vexa-fork (from `development`), talke and
aw-notetaker (both from `origin/main`) · vexa-fork and talke committed and pushed for the image
build and the worker; aw-notetaker waits for the image tag (§5). Not deployed.

Read this next to the [design](2026-11-14-livekit-bot-design.md) (the spec; §3.1 holds the live
step-0 results) and the [plan](2026-11-14-livekit-bot-plan.md) (task numbers below are its).

## 1. Summary

| | |
|---|---|
| Tasks done | 1–14 of 15; this report is 15. Deploy and the live tests wait on the owner (§5). |
| Change size | vexa-fork 53 files (+751 −77) plus new: `modules/livekit-capture/`, `modules/join/src/livekit/`, `join/src/shared/recording-notice.ts`, two bot tests, the invocation golden, decision 0017, a changelog fragment. talke 3 files (+17 −8). aw-notetaker 19 files (+163 −15). New TypeScript in the two LiveKit folders: 973 lines including tests. |
| vexa-fork gates (`node scripts/gates.mjs all`) | 32 green, 3 red, all environment: admin-api pytest and stack-eval (44 identical `DockerException`: the Docker SDK looks for `/var/run/docker.sock`, Docker Desktop serves `~/.docker/run/docker.sock`; admin-api is unchanged on this branch) and the compose stack proof (`minio/mc` pull access denied, before any image build). |
| Tests | record-chunker, livekit-capture, join (knock 24, livekit join 15), bot (35 files incl. the new L3 LiveKit channel test and the redaction test; real Chromium, no skips) all green. meeting-api 2852 passed, 367 skipped. Exporter 429 passed; black, ruff, mypy clean. |
| Other repos | talke worker 216 passed, 9 errors that are identical on `origin/main` (`test_worker.py` recovery tests). calendar-dispatcher 504 passed; black, ruff, `mypy calendar_dispatcher/` clean (`mypy .` reports 2 test-file errors that `origin/main` has too). notetaker-external-api 432 passed; black, ruff, mypy clean. Portal 3023 passed, 24 failed (all in Postgres-backed files, no local database; files untouched), `tsc` and lint clean. |
| Reviews | `/ponytail:ponytail-audit`, `/ponytail:ponytail-review`, then four fresh reviewers (bot/page, meeting-api/exporter/Helm, worker/calendar/portal, security). Every finding they raised was fixed or ruled on (§4). |

What it does, end to end: a link `https://meet-next.olamee.com/<abc-defg-hij>` from a calendar
invite or the portal becomes an aw-bots `livekit` meeting (host listed in `VEXA_LIVEKIT_HOSTS`). The
bot asks Meet's waiting room to enter as the configured bot name, waits for the host, then opens a
page it serves itself and joins the LiveKit room audio-only. It posts the recording notice in
Meet's chat (and to each later joiner), records each person on a channel named from LiveKit
(`channel_kind: livekit`, participant sid, name), and leaves when removed, disconnected, or alone.
The exporter indexes the channels and the worker names each person from their own channel.

## 2. Task by task

| Task | Built | Verified | Differs from plan? |
|---|---|---|---|
| 0 (design step 0) | Live check on `meet-next` from the bot pool | Results in design §3.1 | Findings A–D changed §4.3–4.5 (owner approved) |
| 1 Contracts | `livekit` in invocation.v1, api.v1, ws.v1; golden; re-sealed | `gate:schema`, `gate:contract-version` | No |
| 2 Channel targets | `selectLivekitChannels`, `createLivekitChannelNumbers` (`forget`) | record-chunker test | No |
| 3 Page module | `@vexa/livekit-capture` (session, speakers, roster, chat), separate `livekit-page.global.js`, CALM node | module test, isolation, licences, dataflow | Licence classifier entry (§3) |
| 4 Knock | `knockUntilAdmitted`, `parseLivekitUrl`, Meet answer fixtures | knock test | Fixture values synthetic (§3) |
| 5 Join | page on `https://livekit.aw-bots.invalid`, connect, removal monitor, leave, notice | join test in Chromium | Notice text moved to `shared/` (§3) |
| 6 Bot | mixed lane, channel scan, hints, roster, join-driver branches | bot suite incl. new L3 test | Immediate release on unsubscribe (§3) |
| 7 meeting-api | link rule, spawn host check, stop, channel kind, `VEXA_LIVEKIT_HOSTS` (config, Helm, compose) | 2852 passed, helm test, config gate | Spawn check added (design §5 assumed one existed) |
| 8 Exporter | `channel_kind: livekit` accepted | exporter suite | No |
| 9 talke worker | a `livekit` channel is named like `jitsi` | worker tests | §6.3 doc line not written yet (§5) |
| 10 Calendar | `_livekit_url_re`, `AW_BOTS_LIVEKIT_HOSTS` | 504 passed | Stricter than plan (https, no port, ASCII, no trailing path) |
| 11 API, portal | external `livekit` + guide row; portal label "Olamee" | suites, tsc, lint | `BotJob` (retired path) not changed |
| 12 Deploy, docs | values, calendar Deployment, runbook, handbook, 0017, changelogs, CLAUDE.md | readme, docs-version gates | No |
| 13 Verify | §1 | §1 | No |
| 14 Review | §4 | each fix RED→GREEN | Owner asked to implement the nice-to-have too |

## 3. Deviations from the plan and why

- **A separate page bundle** keeps `livekit-client` (~1 MB) out of every other platform's pages.
- **Licence gate:** `@bufbuild/protobuf` declares `(Apache-2.0 AND BSD-3-Clause)`; the classifier
  gained that one exact expression (both Category A).
- **Channels:** a LiveKit channel is a subscribed track keyed `<sid>|<track id>`, released at once
  when unsubscribed, because LiveKit gives a freed receiver to the next subscription.
- **Notice text** moved to `join/src/shared/recording-notice.ts` (platform folders may not import
  each other); Meet still exports `MEET_RECORDING_NOTICE`, unchanged.
- **Knock fixtures** carry the key set Meet 1.37 returned live; their values are synthetic.
- **talke §6.3 line** (re-run the knock test before a Meet upgrade) is not written: one checkout
  without commits cannot hold edits on two branches; it goes on `feat/livekit` once the worker
  branch is committed.

## 4. Every ruling made on the owner's behalf

From the reviews (all fixed, each with a test that failed first):

| Severity | Finding | Fix |
|---|---|---|
| Critical | The LiveKit token could reach Loki: Chrome logs a failed WebSocket's URL (`access_token` in the query) and the bridge forwards console lines containing "fail" | `redactPageConsole` in the forwarder (also redacts a LiveKit identity holding an email) |
| Important | Encrypted-room guard could never fire (`room.isE2EEEnabled` is the bot's own state) | Encryption read from remote publications; encrypted tracks never subscribed |
| Important | Knock followed redirects off the listed host | `redirect: "error"`; non-JSON answer → typed failure without the body |
| Important | Notice reached only people present at connect | Resent to each later joiner, addressed to them only |
| Important | Backoff (10 s) exceeded Meet's 6 s waiting drop | 4 s, then 5 s |
| Must (audit/review) | "People are here" rule skipped the deaf-bot repair | Roster vetoes only while frames arrive |
| Should | Port, `http`, `;params`, trailing-dot host on a LiveKit link | Refused in meeting-api, the bot and the calendar module |
| Owner request | A silent meeting with people present lost the bot after 10 min | `exact-roster` leave rule, LiveKit only |
| Minor | Same-name speakers collapsed; roster ignored renames; scan order on a reused receiver; key left after an ended track; silent missing-session skip; page entry outside the front door; `livekit.url` unchecked; Kelvin sign / loose trailing path in the calendar pattern | All fixed |

Decided and left as is:

- **Spawn does not re-check the host** after it is removed from `VEXA_LIVEKIT_HOSTS` (documented in
  meeting-lifecycle.md: remove a host only when nothing on it is planned). Cost if wrong: one
  planned meeting on a just-delisted host still gets its bot.
- **The bot does not check the host list itself**; meeting-api is the gate on all four entry points
  (verified by two reviewers).
- **A dropped connection** ends the bot as `evicted`, as on every platform (owner).
- **`meet.olamee.com` in both calendar lists at once** labels the link Jitsi (aw-bots still reads
  LiveKit); the runbook says move it in one release.
- **MCP link parser** not updated (upstream service, not on AW's production path).

## 5. What needs the owner

1. **Commits.** vexa-fork and talke are pushed. aw-notetaker is committed and pushed once, with the
   new image tag in `deployment/base/aw-bots/values.yaml`, after you merge into `development` and
   share the tag.
2. **Deploy, in this order:** worker → aw-bots (image from `aw-images.yml` on push to
   `development`, then the meeting-api values, then the exporter) → calendar module → portal.
   The calendar Deployment before aw-bots makes aw-bots refuse LiveKit entries until it is updated.
3. **Live tests** (design §8), plus what step 0 could not show: two people speaking, a denial, the
   10-minute timeout, a reconnect, a 15-person call's bot CPU, a later joiner seeing the notice, and
   whether a signed-in Meet user without a display name sends an email as `participant.name`.
4. **Before this file is deleted:** move what stays true into decision 0017 and the handbook (most
   already is), per the design's header.

## 6. How to run

```
cd vexa-fork
pnpm --filter @vexa/record-chunker --filter @vexa/livekit-capture --filter @vexa/join --filter @vexa/bot test
node scripts/gates.mjs all
(cd core/meetings/services/meeting-api && uv run pytest -q)
(cd integrations/out/aw-notetaker && uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter)
```

The bot's browser tests need Playwright's Chromium (`npx playwright install chromium`). The talke
worker has no local environment: a Python 3.11 venv with `requirements.txt` and
`requirements-test.txt`, then `python -m pytest -q tests`. aw-notetaker modules run from the
repo-root `.venv` (README of each module); the portal needs `npm ci`.
