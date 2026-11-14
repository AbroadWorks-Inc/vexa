/**
 * Page console lines the bridge forwards to the container log must never carry a LiveKit token
 * (Chrome prints a failed WebSocket's URL, query included) or a LiveKit identity that holds an
 * email. Run: npx tsx src/page-console-redaction.test.ts
 */
import { redactPageConsole } from './capture-bridge.js';

let failed = 0;
const check = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};

const ws = "WebSocket connection to 'wss://livekit.example.org/rtc?access_token=eyJhbGciOi.payload.sig&auto_subscribe=0' failed";
const out = redactPageConsole(ws);
check('a token in a failed WebSocket URL is redacted', !out.includes('eyJhbGciOi') && out.includes('access_token=<redacted>&auto_subscribe=0'), out);
const id = redactPageConsole('unable to subscribe {remoteParticipant: f:03fed54f-9364:asha@example.com, trackSid: TR_1}');
check('a LiveKit identity holding an email is redacted', !id.includes('asha@example.com') && id.includes('trackSid: TR_1'), id);
const plain = '[perspeaker] capture started with 3 stream(s)';
check('any other line is unchanged', redactPageConsole(plain) === plain);

if (failed) { console.error(`\n❌ page console redaction: ${failed} check(s) FAILED.`); process.exit(1); }
console.log('\n✅ page console redaction: tokens and identities never reach the container log.');
