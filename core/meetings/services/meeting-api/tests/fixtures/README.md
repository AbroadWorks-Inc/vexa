# meeting-api tests: fixtures

Data files the meeting-api tests read.

- `meeting-url-redaction-vectors.json` — the table `src/meeting_api/meeting_url_redaction.py` is held
  to, and its TypeScript twin `@vexa/join` `src/shared/url-redaction.ts` too. Byte-identical to the
  join module's copy; `tests/test_meeting_url_secrets.py` fails when they differ.
