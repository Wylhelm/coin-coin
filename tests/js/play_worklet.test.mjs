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

test("played progress reports every 100 ms at the actual AudioContext rate", () => {
  for (const rate of [16_000, 24_000, 48_000]) {
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
