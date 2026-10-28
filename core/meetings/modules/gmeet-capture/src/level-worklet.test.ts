/**
 * The loudness worklet (LEVEL_WORKLET_SRC) run as written, outside a browser: a fake
 * AudioWorkletProcessor realm, 4096 samples in, one [rms, peak, samples] posted.
 */
import { LEVEL_WORKLET_SRC } from './pcm-capture.js';

let failed = 0;
const check = (label: string, ok: boolean, detail = ''): void => {
  if (ok) console.log(`  ✅ ${label}`);
  else { failed++; console.error(`  ❌ ${label} ${detail}`); }
};

const posted: unknown[] = [];
let Processor: new () => { process(inputs: Float32Array[][]): boolean };
class AudioWorkletProcessor { port = { postMessage: (m: unknown) => posted.push(m) }; }
const registerProcessor = (_name: string, cls: typeof Processor): void => { Processor = cls; };
new Function('AudioWorkletProcessor', 'registerProcessor', LEVEL_WORKLET_SRC)(AudioWorkletProcessor, registerProcessor);

const node = new Processor!();
const quantum = 128;
const block = new Float32Array(4096).map((_, i) => (i % 2 ? 0.5 : -0.5));
block[100] = -0.9;
for (let i = 0; i < block.length; i += quantum) node.process([[block.subarray(i, i + quantum)]]);

check('one message per 4096-sample block', posted.length === 1, JSON.stringify(posted));
const [rms, peak, samples] = posted[0] as number[];
const expected = Math.sqrt((4095 * 0.25 + 0.81) / 4096);
check('rms of the block', Math.abs(rms - expected) < 1e-9, String(rms));
check('peak is the largest magnitude', Math.abs(peak - 0.9) < 1e-6, String(peak));
check('samples counted', samples === 4096, String(samples));
node.process([[new Float32Array(quantum)]]);
check('a partial block posts nothing', posted.length === 1);

if (failed) { console.error(`\n❌ level worklet: ${failed} check(s) FAILED.`); process.exit(1); }
console.log('\n✅ level worklet: one loudness value per block, computed on the audio thread.');
