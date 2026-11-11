// Hardware-decoded H.264 screen mirror (WebCodecs) for the dashboard canvas.
// Packet format (binary WebSocket message), see server/app/services/media_packets.py:
//   [u8 kind][u8 idLen][id utf-8][u64 BE pts µs][payload]   kind 1 = config (SPS/PPS), 2 = key, 3 = delta
// Payloads are Annex-B; with no `description`, WebCodecs treats the AVC bitstream as Annex-B.

const MEDIA_KIND = { CONFIG: 1, KEY: 2, DELTA: 3 };
const _utf8 = new TextDecoder();

function parseMediaPacket(buf) {
  const v = new DataView(buf);
  const kind = v.getUint8(0);
  const n = v.getUint8(1);
  const deviceId = _utf8.decode(new Uint8Array(buf, 2, n));
  const ptsUs = v.getUint32(2 + n) * 4294967296 + v.getUint32(6 + n);
  return { kind, deviceId, ptsUs, payload: new Uint8Array(buf, 10 + n) };
}

// First SPS NAL unit (starting at its NAL header byte) in an Annex-B buffer, or null
function findSpsNal(annexb) {
  for (let i = 0; i + 3 < annexb.length; i++) {
    if (annexb[i] === 0 && annexb[i + 1] === 0 && annexb[i + 2] === 1) {
      const start = i + 3;
      if ((annexb[start] & 0x1f) === 7) return annexb.subarray(start, Math.min(annexb.length, start + 4));
      i = start;
    }
  }
  return null;
}

function avcCodecString(spsNal) {
  const hex = (b) => b.toString(16).padStart(2, "0");
  return "avc1." + hex(spsNal[1]) + hex(spsNal[2]) + hex(spsNal[3]);  // profile, constraints, level
}

class H264Player {
  static supported() {
    return typeof window.VideoDecoder === "function" && window.isSecureContext === true;
  }

  // Can this browser really decode the phone's H.264? (VideoDecoder existing is not enough: Linux without
  // VA-API, VMs and some Chromium builds have it but can't decode High profile.)
  static probe() {
    if (!H264Player.supported()) return Promise.resolve(false);
    if (!H264Player._probe) {
      const tries = ["avc1.640028", "avc1.4d0028", "avc1.42e01f"].map((codec) =>
        VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true }).then((r) => !!r.supported, () => false));
      H264Player._probe = Promise.all(tries).then((r) => r[0] || r[1] || r[2]);
    }
    return H264Player._probe;
  }

  constructor(canvas, { onFirstFrame, onStuck, onUnsupported } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onFirstFrame = onFirstFrame;
    this.onStuck = onStuck;          // decoder far behind / errored: ask the server to resync this viewer
    this.onUnsupported = onUnsupported;  // keeps failing: let the page fall back to snapshots
    this._errors = [];
    this._lastStuck = 0;

    this.decoder = null;
    this.config = null;
    this.needKey = true;
    this.frames = 0;
    this.dropped = 0;
    this.rotation = 0;               // degrees clockwise to turn the picture upright (camera sensors are sideways)
    this._fpsFrames = 0;
    this._fpsAt = performance.now();
    this.fps = 0;
  }

  push(pkt) {
    if (pkt.kind === MEDIA_KIND.CONFIG) return this._configure(pkt.payload);
    if (pkt.kind !== MEDIA_KIND.KEY && pkt.kind !== MEDIA_KIND.DELTA) return;
    if (!this.decoder || this.decoder.state !== "configured") {
      // frames but no config (joined mid-stream / after a reset): ask for config + current group (rate-limited)
      this._stuck();
      return;
    }
    const isKey = pkt.kind === MEDIA_KIND.KEY;
    if (this.needKey && !isKey) { this.dropped++; return; }
    // A resync replays a whole picture group at once, so a short backlog is normal; only a backlog that
    // lasts a full second means this browser can't keep up → start over from the server's cache.
    const now = performance.now();
    if (this.decoder.decodeQueueSize > 10) {
      if (!this._backlogSince) this._backlogSince = now;
      if (now - this._backlogSince > 1000) {
        this._backlogSince = 0;
        this.dropped++;
        this._restart();
        return;
      }
    } else {
      this._backlogSince = 0;
    }
    let data = pkt.payload;
    if (isKey && this.config) {                     // key frames carry SPS/PPS in front (Annex-B)
      data = new Uint8Array(this.config.length + pkt.payload.length);
      data.set(this.config, 0);
      data.set(pkt.payload, this.config.length);
    }
    try {
      this.decoder.decode(new EncodedVideoChunk({ type: isKey ? "key" : "delta", timestamp: pkt.ptsUs, data }));
      if (isKey) this.needKey = false;
    } catch (e) {
      console.warn("[Mirror] decode failed:", e);
      this._failed();
    }
  }

  _configure(cfg) {
    const sps = findSpsNal(cfg);
    if (!sps) return;
    this.config = cfg.slice();                      // copy: the payload is a view into the WebSocket buffer
    if (!this.decoder || this.decoder.state === "closed") {
      this.decoder = new VideoDecoder({
        output: (frame) => this._draw(frame),
        error: (e) => { console.warn("[Mirror] decoder error:", e); this._failed(); },
      });
    }
    try {
      // no hardware-only hint: the browser uses the hardware decoder when it has one, software otherwise
      this.decoder.configure({ codec: avcCodecString(sps), optimizeForLatency: true });
    } catch (e) {
      this._failed();
      return;
    }

    this.needKey = true;
  }

  setRotation(degrees) {
    this.rotation = ((Number(degrees) || 0) % 360 + 360) % 360;
  }

  _draw(frame) {
    const w = frame.displayWidth, h = frame.displayHeight;
    const sideways = this.rotation === 90 || this.rotation === 270;
    const cw = sideways ? h : w, ch = sideways ? w : h;
    if (this.canvas.width !== cw || this.canvas.height !== ch) {   // first frame, rotation or lens switch
      this.canvas.width = cw;
      this.canvas.height = ch;
    }
    try {
      if (this.rotation) {
        this.ctx.save();
        this.ctx.translate(cw / 2, ch / 2);
        this.ctx.rotate((this.rotation * Math.PI) / 180);
        this.ctx.drawImage(frame, -w / 2, -h / 2, w, h);
        this.ctx.restore();
      } else {
        this.ctx.drawImage(frame, 0, 0, w, h);
      }
    } finally {
      frame.close();                                 // a leaked frame stalls the decoder's small output pool
    }
    this.frames++;
    this._fpsFrames++;
    const now = performance.now();
    if (now - this._fpsAt >= 1000) {
      this.fps = (this._fpsFrames * 1000) / (now - this._fpsAt);
      this._fpsFrames = 0;
      this._fpsAt = now;
    }
    if (this.frames === 1 && this.onFirstFrame) this.onFirstFrame();
  }

  _failed() {
    const now = performance.now();
    this._errors = this._errors.filter((t) => now - t < 10000);
    this._errors.push(now);
    if (this._errors.length >= 3 && this.onUnsupported) {   // 3 failures in 10 s: this browser can't do it
      this.close();
      this.onUnsupported();
      return;
    }
    this._restart();
  }

  _stuck() {
    const now = performance.now();
    if (now - this._lastStuck < 1000) return;       // at most one resync request a second
    this._lastStuck = now;
    if (this.onStuck) this.onStuck();
  }

  _restart() {
    this.close();
    this._stuck();
  }

  close() {
    try { if (this.decoder && this.decoder.state !== "closed") this.decoder.close(); } catch (e) {}
    this.decoder = null;
    this.config = null;
    this.needKey = true;
    this.frames = 0;
  }

  get stats() {
    return { fps: Math.round(this.fps * 10) / 10, frames: this.frames, dropped: this.dropped,
             decodeQueue: this.decoder ? this.decoder.decodeQueueSize : 0 };
  }
}

window.parseMediaPacket = parseMediaPacket;
window.avcCodecString = avcCodecString;
window.H264Player = H264Player;
