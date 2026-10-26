// PCM ownership shared by the AudioWorklet and deterministic browser tests.
export class PCMQueue {
  constructor(retire = () => {}) {
    this.retire = retire;
    this.chunks = [];
    this.frames = 0;
    this.epoch = null;
    this.started = false;
    this.eos = false;
    this.waiting = false;
    this.prebufferWait = 0;
    this.played = this.underrun = this.dropped = this.nonzero = 0;
  }
  reset(epoch = null) {
    for (const c of this.chunks) this.retire(c.sequence);
    this.chunks = [];
    this.frames = 0;
    this.started = false;
    this.eos = false;
    this.waiting = false;
    this.prebufferWait = 0;
    this.epoch = epoch;
  }
  push(packet) {
    if (!(packet.pcm instanceof ArrayBuffer) || !Number.isInteger(packet.frames) ||
        packet.frames < 0 || packet.frames > 960 || packet.pcm.byteLength !== packet.frames * 4 ||
        (!packet.frames && !packet.eos)) throw new Error('Invalid worklet PCM');
    if (packet.epoch !== this.epoch || packet.discontinuity) {
      this.dropped += this.frames;
      this.reset(packet.epoch);
    }
    this.chunks.push({...packet, offset: 0, view: new DataView(packet.pcm)});
    this.frames += packet.frames;
    this.eos ||= packet.eos;
    if (this.frames > 9600 || this.chunks.length > 10) {
      // Preserve the newest <=60 ms and discard stale presentation data.
      while (this.chunks.length && (this.frames > 2880 || this.chunks.length > 10)) {
        const old = this.chunks.shift();
        this.frames -= old.frames - old.offset;
        this.dropped += old.frames - old.offset;
        this.retire(old.sequence);
      }
      this.started = false;
    }
  }
  render(left, right) {
    left.fill(0); right.fill(0);
    if (!this.started && this.frames > 0) this.prebufferWait += left.length;
    // A native pause may leave less than60 ms without ending the track.
    // Bound waiting by render time so that prefix is consumed and acknowledged.
    if (!this.started && (this.frames >= 2880 ||
        (this.frames > 0 && (this.eos || this.prebufferWait >= 2880)))) {
      this.started = true; this.waiting = false; this.prebufferWait = 0;
    }
    if (!this.started) {
      if (this.waiting && !this.eos) this.underrun += left.length;
      while (this.chunks.length && this.chunks[0].frames === 0) this.retire(this.chunks.shift().sequence);
      return;
    }
    let out = 0;
    while (out < left.length && this.chunks.length) {
      const chunk = this.chunks[0];
      const count = Math.min(left.length - out, chunk.frames - chunk.offset);
      for (let i = 0; i < count; i++) {
        const offset = (chunk.offset + i) * 4;
        const l = chunk.view.getInt16(offset, true), r = chunk.view.getInt16(offset+2, true);
        left[out+i] = l / 32768; right[out+i] = r / 32768;
        this.nonzero += (l !== 0) + (r !== 0);
      }
      out += count;
      this.frames -= count;
      this.played += count;
      chunk.offset += count;
      if (chunk.offset === chunk.frames) this.retire(this.chunks.shift().sequence);
    }
    if (out < left.length || (!this.frames && this.eos)) {
      if (!this.eos) this.underrun += left.length - out;
      this.waiting = !this.eos;
      this.started = false;
    }
  }
  stats() {
    return {played: this.played, nonzero: this.nonzero, underrun: this.underrun, dropped: this.dropped,
            buffered: this.frames, chunks: this.chunks.length, playing: this.started, eos: this.eos};
  }
}
