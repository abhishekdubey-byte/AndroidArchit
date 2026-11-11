// The phone microphone in the dashboard: AAC packets (media kinds 4 = AudioSpecificConfig, 5 = AAC frame) decoded
// with WebCodecs AudioDecoder and played by the "pcm-player" AudioWorklet (80 ms jitter buffer, see audio-worklet.js).
// `clock()` returns the phone timestamp (µs) being heard right now, or null when no audio is playing; the camera
// player uses it to show each video frame when its sound is heard (one clock: the phone's).
//
// Create it inside a click handler: browsers only let an AudioContext start from a user gesture.

const AUDIO_KIND = { CONFIG: 4, FRAME: 5 };

class AacPlayer {
  static supported() {
    return typeof window.AudioDecoder === "function" && typeof window.AudioWorkletNode === "function"
      && window.isSecureContext === true;
  }

  constructor({ targetMs = 80, maxTargetMs = 200 } = {}) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctx({ sampleRate: 48000, latencyHint: "interactive" });
    if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
    this.node = null;
    this.decoder = null;
    this.asc = null;
    this.closed = false;
    this.pending = [];                         // decoded audio waiting for the worklet to load
    this._report = null;                       // last {pts, time} from the worklet
    this._reportAt = 0;
    this.stats = { buffered: 0, underruns: 0, droppedMs: 0, decoded: 0 };
    this.ready = this.ctx.audioWorklet.addModule("/js/audio-worklet.js?v=1.9.47").then(() => {
      if (this.closed) return;
      this.node = new AudioWorkletNode(this.ctx, "pcm-player", {
        numberOfInputs: 0, outputChannelCount: [1], processorOptions: { targetMs, maxTargetMs } });
      this.node.port.onmessage = (e) => {
        const m = e.data;
        if (m.type !== "clock") return;
        this._report = m;
        this._reportAt = performance.now();
        this.stats.buffered = Math.round(m.buffered * 1000);
        this.stats.underruns = m.underruns;
        this.stats.droppedMs = Math.round(m.droppedMs);
        this.stats.targetMs = Math.round(m.targetMs);
      };
      this.node.connect(this.gain);
      for (const p of this.pending) this.node.port.postMessage(p, [p.data.buffer]);
      this.pending = [];
    }).catch((e) => { console.warn("[Audio] worklet failed:", e); });
  }

  push(pkt) {
    if (this.closed) return;
    if (pkt.kind === AUDIO_KIND.CONFIG) return this._configure(pkt.payload);
    if (pkt.kind !== AUDIO_KIND.FRAME || !this.decoder || this.decoder.state !== "configured") return;
    if (this.decoder.decodeQueueSize > 50) return;             // decoder hopelessly behind: skip, never pile up
    try {
      this.decoder.decode(new EncodedAudioChunk({ type: "key", timestamp: pkt.ptsUs, data: pkt.payload }));
    } catch (e) {
      console.warn("[Audio] decode failed:", e);
    }
  }

  _configure(asc) {
    const same = this.asc && this.asc.length === asc.length && this.asc.every((b, i) => b === asc[i]);
    if (same && this.decoder && this.decoder.state === "configured") return;
    this.asc = asc.slice();
    const rates = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];
    const bits = (asc[0] << 8) | asc[1];
    const sampleRate = rates[(bits >> 7) & 0x0f] || 48000;
    const numberOfChannels = (bits >> 3) & 0x0f || 1;
    try {
      if (!this.decoder || this.decoder.state === "closed") {
        this.decoder = new AudioDecoder({
          output: (data) => this._out(data),
          error: (e) => { console.warn("[Audio] decoder error:", e); this.decoder = null; this.asc = null; },
        });
      }
      this.decoder.configure({ codec: "mp4a.40.2", sampleRate, numberOfChannels, description: this.asc });
    } catch (e) {
      console.warn("[Audio] AAC not supported here:", e);
      this.decoder = null;
    }
  }

  _out(data) {
    try {
      const n = data.numberOfFrames;
      const pcm = new Float32Array(n);
      data.copyTo(pcm, { planeIndex: 0, format: "f32-planar" });   // first channel (the phone sends mono)
      const msg = { type: "pcm", data: pcm, pts: data.timestamp };
      this.stats.decoded++;
      if (this.node) this.node.port.postMessage(msg, [pcm.buffer]);
      else if (this.pending.length < 20) this.pending.push(msg);
    } finally {
      data.close();
    }
  }

  // Phone timestamp (µs) of the sound reaching the speakers now; null if nothing has played for 300 ms.
  clock() {
    const r = this._report;
    if (!r || this.closed || this.ctx.state !== "running" || performance.now() - this._reportAt > 300) return null;
    const out = (this.ctx.outputLatency || 0) + (this.ctx.baseLatency || 0);
    return r.pts + (this.ctx.currentTime - r.time - out) * 1e6;
  }

  setMuted(muted) {
    // muting keeps the clock running, so the picture stays smooth and in sync
    this.gain.gain.value = muted ? 0 : 1;
  }

  suspend() { if (!this.closed && this.ctx.state === "running") this.ctx.suspend().catch(() => {}); }
  resume() { if (!this.closed && this.ctx.state === "suspended") this.ctx.resume().catch(() => {}); }

  close() {
    this.closed = true;
    try { if (this.decoder && this.decoder.state !== "closed") this.decoder.close(); } catch (e) {}
    this.decoder = null;
    try { if (this.node) this.node.disconnect(); } catch (e) {}
    this.ctx.close().catch(() => {});
  }
}

// The laptop microphone for phone calls: echo-cancelled, noise-suppressed, 16 kHz PCM16 blocks via an AudioWorklet
// (replaces the deprecated ScriptProcessor). `onBlock(Int16Array, level)` gets 40 ms at a time.
class LaptopMic {
  static async start(onBlock) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx({ latencyHint: "interactive" });
    try {
      await ctx.audioWorklet.addModule("/js/audio-worklet.js?v=1.9.47");
      const src = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, "mic-capture", { numberOfOutputs: 0 });
      node.port.onmessage = (e) => { if (e.data.type === "pcm") onBlock(e.data.pcm, e.data.level); };
      src.connect(node);
      if (ctx.state === "suspended") await ctx.resume();
      return { stop: () => { stream.getTracks().forEach((t) => t.stop()); try { src.disconnect(); } catch (e) {} ctx.close().catch(() => {}); } };
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
      throw e;
    }
  }
}

window.AacPlayer = AacPlayer;
window.LaptopMic = LaptopMic;
