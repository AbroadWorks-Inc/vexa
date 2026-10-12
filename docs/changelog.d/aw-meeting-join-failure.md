- **meeting-api: the v2 meeting says why a bot never got in.** The meeting object gains
  `join_failure` after `failure_stage`: `{reason, attribution, detail}` from the failed bot's join
  evidence, or `null`. A portal can now tell Zoom refusing the bot (`exogenous_platform`) from a
  host declining it (`host_action`). While a meeting waits for a new bot, and once one is sent, the
  failed bot's evidence is kept on the retry marker and then in the meeting's history, so a meeting
  never shows an earlier bot's refusal. intake.v1 `Meeting` has 20 keys; meeting-api's
  `V2-API.md` documents the field.
