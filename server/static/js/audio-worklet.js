// AudioWorklet processors for the dashboard (run on the browser's audio thread).
//
// "pcm-player": plays the phone microphone (decoded AAC, mono Float32) through a small jitter buffer.
//   - starts (and restarts after an underrun) once `targetMs` of audio is queued (80 ms);
//   - if more than `maxMs` is queued (200 ms, e.g. after a network stall), the oldest audio is dropped back to the
//     target, so the listener hears the present instead of a growing delay;
//   - short fades at underruns/restarts avoid clicks;
//   - every ~50 ms it reports which phone timestamp is playing ({pts, time}) so video can be timed against it.
//
// "mic-capture": the laptop microphone as 16 kHz PCM16 blocks (40 ms) for the phone call bridge, plus a level.

const FADE = 64;

class PcmPlayer extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.target = Math.round(sampleRate * (o.targetMs || 80) / 1000);
    this.max = Math.round(sampleRate * (o.maxMs || 200) / 1000);
    this.chunks = [];          // { data: Float32Array, pts: µs of data[0], off }
    this.buffered = 0;
    this.playing = false;
    this.fadeIn = 0;
    this.lastReport = -1;
    this.underruns = 0;
    this.dropped = 0;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === "pcm") {
        this.chunks.push({ data: m.data, pts: m.pts, off: 0 });
        this.buffered += m.data.length;
        this.trim();
      } else if (m.type === "reset") {
        this.chunks = [];
        this.buffered = 0;
        this.playing = false;
      }
    };
  }

  trim() {
    if (this.buffered <= this.max) return;
    let drop = this.buffered - this.target;
    this.dropped += drop;
    while (drop > 0 && this.chunks.length) {
      const c = this.chunks[0];
      const left = c.data.length - c.off;
      if (left <= drop) { this.chunks.shift(); drop -= left; this.buffered -= left; }
      else { c.off += drop; this.buffered -= drop; drop = 0; }
    }
    this.fadeIn = FADE;
  }

  process(inputs, outputs) {
    const outs = outputs[0];
    if (!outs || !outs.length) return true;
    const out = outs[0];
    if (!this.playing && this.buffered >= this.target) { this.playing = true; this.fadeIn = FADE; }
    let i = 0;
    let headPts = null;
    if (this.playing) {
      while (i < out.length && this.chunks.length) {
        const c = this.chunks[0];
        if (headPts === null) headPts = c.pts + (c.off * 1e6) / sampleRate;
        const n = Math.min(out.length - i, c.data.length - c.off);
        out.set(c.data.subarray(c.off, c.off + n), i);
        i += n; c.off += n; this.buffered -= n;
        if (c.off >= c.data.length) this.chunks.shift();
      }
      if (i < out.length) {                       // ran dry: fade out what we have, wait for the target again
        this.playing = false;
        this.underruns++;
        const f = Math.min(FADE, i);
        for (let k = 0; k < f; k++) out[i - f + k] *= 1 - (k + 1) / f;
      }
    }
    if (this.fadeIn > 0) {
      const n = Math.min(this.fadeIn, i);
      for (let k = 0; k < n; k++) out[k] *= (FADE - this.fadeIn + k + 1) / FADE;
      this.fadeIn -= n;
    }
    for (; i < out.length; i++) out[i] = 0;
    for (let ch = 1; ch < outs.length; ch++) outs[ch].set(out);
    if (headPts !== null && currentTime - this.lastReport >= 0.05) {
      this.lastReport = currentTime;
      this.port.postMessage({ type: "clock", pts: headPts, time: currentTime, buffered: this.buffered / sampleRate,
                              underruns: this.underruns, droppedMs: (this.dropped * 1000) / sampleRate });
    }
    return true;
  }
}

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.block = new Int16Array(640);            // 40 ms at 16 kHz
    this.n = 0;
    this.sum = 0;
    this.cnt = 0;
    this.frac = 0;
    this.sq = 0;
    this.sqN = 0;
  }

  process(inputs) {
    const inp = inputs[0] && inputs[0][0];
    if (!inp) return true;
    for (let i = 0; i < inp.length; i++) {
      const x = inp[i];
      this.sq += x * x; this.sqN++;
      this.sum += x; this.cnt++;                 // box-filter decimation to 16 kHz (fine for speech)
      this.frac += 1;
      if (this.frac >= this.ratio) {
        this.frac -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.sum / this.cnt));
        this.sum = 0; this.cnt = 0;
        this.block[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.block.length) {
          const pcm = this.block;
          this.port.postMessage({ type: "pcm", pcm, level: Math.sqrt(this.sq / Math.max(1, this.sqN)) }, [pcm.buffer]);
          this.block = new Int16Array(640);
          this.n = 0; this.sq = 0; this.sqN = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
registerProcessor("mic-capture", MicCapture);
