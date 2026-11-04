export class AudioSequenceGate {
  constructor() {
    this.discardThrough = 0n;
  }

  discardThroughSequence(sequence) {
    this.discardThrough = BigInt(sequence);
  }

  decode(packet) {
    if (packet.byteLength < 10) return null;
    const view = new DataView(packet);
    const sequence = view.getBigUint64(0, true);
    if (sequence <= this.discardThrough) return null;
    return { sequence, pcm: packet.slice(8) };
  }
}
