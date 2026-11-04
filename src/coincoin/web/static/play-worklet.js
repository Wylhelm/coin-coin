class CoinCoinPlayProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Float32Array(0);
    this.sourcePosition = 0;
    this.started = false;
    this.playedSamples = 0;
    this.reportSamples = 0;
    this.generation = 0;
    this.port.onmessage = ({ data }) => {
      if (data.type === "frame") {
        const view = new DataView(data.buffer);
        const samples = new Float32Array(view.byteLength / 2);
        for (let i = 0; i < samples.length; i++) {
          samples[i] = view.getInt16(i * 2, true) / 32768;
        }
        const joined = new Float32Array(this.samples.length + samples.length);
        joined.set(this.samples);
        joined.set(samples, this.samples.length);
        this.samples = joined;
      } else if (data.type === "flush") {
        this.samples = new Float32Array(0);
        this.sourcePosition = 0;
        this.started = false;
        this.playedSamples = 0;
        this.reportSamples = 0;
        this.generation = data.generation;
        this.port.postMessage({ type: "flushed", generation: this.generation });
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    output.fill(0);
    if (!this.started && this.samples.length >= 1440) this.started = true;

    let rendered = 0;
    if (this.started) {
      const ratio = 24000 / sampleRate;
      for (let i = 0; i < output.length; i++) {
        if (this.sourcePosition + 1 >= this.samples.length) break;
        const index = Math.floor(this.sourcePosition);
        const fraction = this.sourcePosition - index;
        output[i] =
          this.samples[index] +
          (this.samples[index + 1] - this.samples[index]) * fraction;
        this.sourcePosition += ratio;
        rendered++;
      }
      const consumed = Math.floor(this.sourcePosition);
      this.playedSamples += consumed;
      this.samples = this.samples.slice(consumed);
      this.sourcePosition -= consumed;
    }
    this.reportSamples += output.length;
    if (this.reportSamples >= 2400) {
      this.port.postMessage({
        type: "played",
        ms: Math.floor((this.playedSamples * 1000) / 24000),
        playing: rendered > 0,
        generation: this.generation,
      });
      this.reportSamples = 0;
    }
    return true;
  }
}

registerProcessor("coincoin-play", CoinCoinPlayProcessor);
