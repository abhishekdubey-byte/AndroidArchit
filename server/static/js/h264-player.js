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

  constructor(canvas, { onFirstFrame, onStuck } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onFirstFrame = onFirstFrame;
    this.onStuck = onStuck;          // decoder far behind / errored: ask the server to resync this viewer
    this.decoder = null;
    this.config = null;
    this.needKey = true;
    this.frames = 0;
    this.dropped = 0;
    this._fpsFrames = 0;
    this._fpsAt = performance.now();
    this.fps = 0;
  }

  push(pkt) {
    if (pkt.kind === MEDIA_KIND.CONFIG) return this._configure(pkt.payload);
    if (pkt.kind !== MEDIA_KIND.KEY && pkt.kind !== MEDIA_KIND.DELTA) return;
    if (!this.decoder || this.decoder.state !== "configured") return;
    const isKey = pkt.kind === MEDIA_KIND.KEY;
    if (this.needKey && !isKey) { this.dropped++; return; }
    if (this.decoder.decodeQueueSize > 60) {        // hopelessly behind: start over from the server's cache
      this.dropped++;
      this._restart();
      return;
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
      this._restart();
    }
  }

  _configure(cfg) {
    const sps = findSpsNal(cfg);
    if (!sps) return;
    this.config = cfg.slice();                      // copy: the payload is a view into the WebSocket buffer
    if (!this.decoder || this.decoder.state === "closed") {
      this.decoder = new VideoDecoder({
        output: (frame) => this._draw(frame),
        error: (e) => { console.warn("[Mirror] decoder error:", e); this._restart(); },
      });
    }
    try {
      this.decoder.configure({ codec: avcCodecString(sps), optimizeForLatency: true, hardwareAcceleration: "prefer-hardware" });
    } catch (e) {
      // some browsers reject the hardware hint: let them pick
      this.decoder.configure({ codec: avcCodecString(sps), optimizeForLatency: true });
    }
    this.needKey = true;
  }

  _draw(frame) {
    const w = frame.displayWidth, h = frame.displayHeight;
    if (this.canvas.width !== w || this.canvas.height !== h) {   // first frame or rotation
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.ctx.drawImage(frame, 0, 0, w, h);
    frame.close();
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

  _restart() {
    this.close();
    if (this.onStuck) this.onStuck();
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
