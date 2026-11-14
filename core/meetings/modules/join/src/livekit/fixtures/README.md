# join/src/livekit/fixtures

Meet's waiting-room answers (`POST /api/v1.0/rooms/<code>/request-entry/`), the goldens for
[`knock.test.ts`](../knock.test.ts). The key set is Meet 1.37's, recorded live on 2026-11-14
(`status, username, id, color, entered_at, livekit`; a missing room answers 404 `{detail}`); the
values are synthetic and the token is a placeholder. Before a new Meet version goes live, record
its answers again and run the knock test.
