# join/src/shared — cross-platform join helpers

`escalation.ts` — `startDebugView`: opens the live debug lens (VNC pixels on Linux, CDP
control anywhere) so a human/agent can see and unblock a stuck join (e.g. a reCAPTCHA
block). Only `startDebugView` (the `debug` join option) opens it, bound to loopback (reach it
through a port-forward); a stuck lobby on its own only reports `needs_human_help`, and its lobby
budget is extended by 5 minutes only while the live view is open. Imports host symbols from
`../_host` and Node builtins only.

`url-redaction.ts` — `redactMeetingUrl` / `redactUrlsIn`: every log line and error in this brick
that names a URL names it with its secret query and fragment values masked (Zoom `pwd`, Teams `p`,
Jitsi `jwt`, Zoom `zak`, any `*key*`/`*secret*`/`*token*`), host, path and meeting id kept.
`rethrowRedacted` is the `page.goto(...).catch(...)` that redacts Playwright's own message, which
quotes the URL. Exported as `@vexa/join/url-redaction` for the bot. Held, with the vendored Python
`meeting_url_redaction.py`, to `meeting-url-redaction-vectors.json`.
