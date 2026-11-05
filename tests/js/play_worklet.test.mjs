import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInThisContext } from "node:vm";
import test from "node:test";

let PlayProcessor;
let posted;
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = {
      onmessage: null,
      postMessage(message) {
        posted.push(message);
      },
    };
  }
};
globalThis.registerProcessor = (_name, processor) => {
  PlayProcessor = processor;
};

const workletUrl = new URL("../../src/coincoin/web/static/play-worklet.js", import.meta.url);
runInThisContext(await readFile(workletUrl, "utf8"), { filename: workletUrl.pathname });

const protocolUrl = new URL("../../src/coincoin/web/static/audio-protocol.js", import.meta.url);
const protocolSource = await readFile(protocolUrl, "utf8");
const { AudioSequenceGate } = await import(
  `data:text/javascript;base64,${Buffer.from(protocolSource).toString("base64")}`
);

function makeProcessor(rate) {
  globalThis.sampleRate = rate;
  posted = [];
  return new PlayProcessor();
}

function frame(sequence, samples) {
  const pcm = new Int16Array(samples);
  const packet = new ArrayBuffer(8 + pcm.byteLength);
  new DataView(packet).setBigUint64(0, BigInt(sequence), true);
  new Int16Array(packet, 8).set(pcm);
  return packet;
}

function feed(gate, processor, packet) {
  const decoded = gate.decode(packet);
  if (decoded !== null) {
    processor.port.onmessage({ data: { type: "frame", buffer: decoded.pcm } });
  }
}

function flush(gate, processor, generation, throughSequence) {
  gate.discardThroughSequence(String(throughSequence));
  processor.port.onmessage({
    data: { type: "flush", generation, through_sequence: String(throughSequence) },
  });
  assert.equal(posted.at(-1).type, "flushed");
}

function render(processor, count = 128) {
  const output = new Float32Array(count);
  processor.process([], [[output]]);
  return output;
}

test("played reports retain exact samples for a sub-millisecond tail", () => {
  const processor = makeProcessor(24_000);
  const gate = new AudioSequenceGate();
  feed(gate, processor, frame(1, new Int16Array(25).fill(1000)));
  for (let i = 0; i < 40; i++) render(processor);
  const progress = posted.filter((event) => event.type === "played").at(-1);
  assert.equal(progress.ms, 1);
  assert.equal(progress.samples, 25);
  assert.equal(progress.playing, false);
});

test("flush sequence barrier drops delayed old packets, including repeated flushes", () => {
  const processor = makeProcessor(24_000);
  const gate = new AudioSequenceGate();
  const old = [
    frame(1, new Int16Array(480).fill(1200)),
    frame(2, new Int16Array(480).fill(2400)),
    frame(3, new Int16Array(480).fill(3600)),
  ];
  old.forEach((packet) => feed(gate, processor, packet));

  flush(gate, processor, 1, 3);
  old.forEach((packet) => feed(gate, processor, packet));
  for (let i = 0; i < 15; i++) {
    assert.equal(render(processor).some((sample) => sample !== 0), false);
  }

  const queuedBeforeSecondFlush = frame(4, new Int16Array(480).fill(-1200));
  feed(gate, processor, queuedBeforeSecondFlush);
  flush(gate, processor, 2, 4);
  feed(gate, processor, queuedBeforeSecondFlush);
  for (let i = 0; i < 15; i++) {
    assert.equal(render(processor).some((sample) => sample !== 0), false);
  }

  feed(gate, processor, frame(5, new Int16Array(480).fill(5000)));
  const heard = [];
  for (let i = 0; i < 20 && heard.length < 480; i++) {
    const block = render(processor);
    if (processor.started) heard.push(...block);
  }
  assert.deepEqual(heard.slice(0, 480), new Array(480).fill(5000 / 32768));
});

test("successive non-frame-aligned fragments and a short tail stay contiguous", () => {
  const processor = makeProcessor(24_000);
  const gate = new AudioSequenceGate();
  const fragmentA = Int16Array.from({ length: 400 }, (_, i) => i + 1);
  const fragmentB = Int16Array.from({ length: 400 }, (_, i) => i + 1001);
  const shortTail = Int16Array.from([31, -32, 33]);
  feed(gate, processor, frame(1, fragmentA));
  feed(gate, processor, frame(2, fragmentB));
  feed(gate, processor, frame(3, shortTail));

  const heard = [];
  for (let i = 0; i < 20 && processor.playedSamples < 803; i++) {
    const block = render(processor);
    if (processor.started) heard.push(...block);
  }
  const expected = [...fragmentA, ...fragmentB, ...shortTail].map((sample) => sample / 32768);
  assert.deepEqual(heard.slice(0, expected.length), expected);
  assert.equal(processor.playedSamples, expected.length);
});

test("linear interpolation stays sample-accurate across render blocks", () => {
  for (const rate of [16_000, 24_000, 44_100, 48_000]) {
    const processor = makeProcessor(rate);
    const samples = Int16Array.from({ length: 3000 }, (_, i) => 10 * i - 15_000);
    processor.port.onmessage({ data: { type: "frame", buffer: frame(1, samples).slice(8) } });

    const heard = [];
    for (let i = 0; i < 4; i++) heard.push(...render(processor));
    const ratio = 24_000 / rate;
    for (let i = 0; i < heard.length; i++) {
      const position = i * ratio;
      const index = Math.floor(position);
      const fraction = position - index;
      const left = samples[index] / 32768;
      const right = samples[Math.min(index + 1, samples.length - 1)] / 32768;
      const expected = left + (right - left) * fraction;
      assert.ok(
        Math.abs(heard[i] - expected) < 1e-7,
        `${rate} Hz output ${i}: got ${heard[i]}, expected ${expected}`,
      );
    }
  }

  const processor = makeProcessor(48_000);
  const source = new Int16Array(3000);
  source[63] = -30_000;
  source[64] = 0;
  source[65] = 30_000;
  processor.port.onmessage({ data: { type: "frame", buffer: frame(1, source).slice(8) } });
  render(processor);
  const secondBlock = render(processor);
  assert.ok(Math.abs(secondBlock[1] - 15_000 / 32768) < 1e-7);
});

test("fragment boundaries and recovery from underflow preserve resampling phase", () => {
  for (const rate of [16_000, 24_000, 44_100, 48_000]) {
    const processor = makeProcessor(rate);
    const ratio = 24_000 / rate;
    const initial = Int16Array.from({ length: 1490 }, (_, i) => 1000 + i * 3);
    const tail = Int16Array.from({ length: 1000 }, (_, i) => 6000 + i * 2);
    const joined = Int16Array.from([...initial, ...tail]);
    processor.port.onmessage({ data: { type: "frame", buffer: frame(1, initial).slice(8) } });

    const heard = [...render(processor)];
    processor.port.onmessage({ data: { type: "frame", buffer: frame(2, tail).slice(8) } });
    const outputCount = Math.ceil((initial.length + 100) / ratio);
    while (heard.length < outputCount) heard.push(...render(processor));
    for (let i = 0; i < outputCount; i++) {
      const position = i * ratio;
      const index = Math.floor(position);
      const fraction = position - index;
      const left = joined[index] / 32768;
      const right = joined[Math.min(index + 1, joined.length - 1)] / 32768;
      const expected = left + (right - left) * fraction;
      assert.ok(
        Math.abs(heard[i] - expected) < 1e-7,
        `${rate} Hz fragmented output ${i}: got ${heard[i]}, expected ${expected}`,
      );
    }

    const starved = makeProcessor(rate);
    starved.started = true;
    const shortFragment = Int16Array.from([1000, 2000]);
    starved.port.onmessage({
      data: { type: "frame", buffer: frame(1, shortFragment).slice(8) },
    });
    const underflowBlock = render(starved);
    const phaseAfterUnderflow = starved.sourcePosition;
    assert.ok(underflowBlock.slice(4).every((sample) => sample === 0));

    const resumed = Int16Array.from({ length: 1000 }, (_, i) => 5000 + i * 4);
    starved.port.onmessage({ data: { type: "frame", buffer: frame(2, resumed).slice(8) } });
    const resumedBlock = render(starved);
    for (let i = 0; i < 128; i++) {
      const position = phaseAfterUnderflow + i * ratio;
      const index = Math.floor(position);
      const fraction = position - index;
      const left = resumed[index] / 32768;
      const right = resumed[Math.min(index + 1, resumed.length - 1)] / 32768;
      const expected = left + (right - left) * fraction;
      assert.ok(
        Math.abs(resumedBlock[i] - expected) < 1e-7,
        `${rate} Hz resumed output ${i}: got ${resumedBlock[i]}, expected ${expected}`,
      );
    }
  }
});

test("played progress reports every 100 ms at the actual AudioContext rate", () => {
  for (const rate of [16_000, 24_000, 44_100, 48_000]) {
    const processor = makeProcessor(rate);
    processor.port.onmessage({
      data: { type: "frame", buffer: frame(1, new Int16Array(24_000)).slice(8) },
    });

    let callbacks = 0;
    while (!posted.some((message) => message.type === "played") && callbacks < 100) {
      render(processor);
      callbacks++;
    }

    assert.ok(posted.some((message) => message.type === "played"), `no report at ${rate} Hz`);
    const elapsedMs = (callbacks * 128 * 1000) / rate;
    const callbackMs = (128 * 1000) / rate;
    assert.ok(elapsedMs >= 100, `${rate} Hz reported early at ${elapsedMs} ms`);
    assert.ok(elapsedMs < 100 + callbackMs, `${rate} Hz reported late at ${elapsedMs} ms`);
  }
});
