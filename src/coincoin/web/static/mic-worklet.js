class CoinCoinMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.previous = 0;
    this.frame = new Int16Array(480);
    this.frameOffset = 0;
    this.levelSamples = 0;
    this.levelSquares = 0;
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    for (const channel of outputs[0] ?? []) channel.fill(0);
    if (!input) return true;

    const ratio = sampleRate / 24000;
    while (this.phase < input.length - 1) {
      const index = Math.floor(this.phase);
      const fraction = this.phase - index;
      const left = index < 0 ? this.previous : input[index];
      const right = index < 0 ? input[0] : input[index + 1];
      const sample = left + (right - left) * fraction;
      this.frame[this.frameOffset++] = Math.max(
        -32768,
        Math.min(32767, Math.round(sample * 32768)),
      );
      this.phase += ratio;
      this.levelSquares += sample * sample;
      this.levelSamples++;
      if (this.frameOffset === this.frame.length) {
        const bytes = new ArrayBuffer(960);
        const view = new DataView(bytes);
        for (let i = 0; i < this.frame.length; i++) view.setInt16(i * 2, this.frame[i], true);
        this.port.postMessage({ type: "frame", buffer: bytes }, [bytes]);
        this.frameOffset = 0;
      }
    }
    this.phase -= input.length;
    this.previous = input[input.length - 1];
    if (this.levelSamples >= 2400) {
      this.port.postMessage({
        type: "level",
        rms: Math.min(1, Math.sqrt(this.levelSquares / this.levelSamples)),
      });
      this.levelSquares = 0;
      this.levelSamples = 0;
    }
    return true;
  }
}

registerProcessor("coincoin-mic", CoinCoinMicProcessor);
