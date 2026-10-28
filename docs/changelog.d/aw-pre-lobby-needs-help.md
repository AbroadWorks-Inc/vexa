- **meeting-api: a blocker met before the waiting room is reported.** `joining → needs_help` is now a
  legal edge (#1251), so the bot's `blocked` report for Zoom's "automated bots aren't allowed" (RTMS)
  wall, a captcha or a consent gate is recorded instead of refused as an illegal transition. A
  failure after such a block is filed at the `joining` stage, never as a lobby the host did not open.
