// ACC cloud relay — a meeting point for the laptop ("hub") and remote phones ("devices").
//
// Both sides connect OUT to this Worker, so phones on mobile data / any Wi-Fi reach a laptop
// that is itself behind NAT. The laptop authenticates with the HUB_KEY secret and pushes SHA-256
// hashes of its current phone/admin tokens, so unauthenticated phones are refused with HTTP 401
// here (no WebSocket, no traffic to the laptop). The laptop still re-checks every phone itself.
//
// Hub frame format (binary, both directions):
//   [1 byte kind][16 bytes ASCII connection id][payload]
//   kind 1 = device opened   (payload: JSON {token, ip})        relay -> hub
//   kind 2 = text message    (payload: UTF-8)                   both
//   kind 3 = binary message  (payload: raw bytes)               both
//   kind 4 = close           (payload: 2-byte close code)       both
//   kind 5 = token hashes    (payload: JSON {hashes: [hex]})    hub -> relay (conn id ignored)
// The hub also sends the text "hb" every 20 s; the runtime answers it without waking the object.
import { DurableObject } from "cloudflare:workers";

const K_OPEN = 1, K_TEXT = 2, K_BIN = 3, K_CLOSE = 4, K_AUTH = 5;
const ID_LEN = 16;
const MAX_DEVICES = 64;
const MAX_DEVICE_MESSAGE = 16 * 1024 * 1024; // hub accepts 17 MiB frames
const HUB_TIMEOUT_MS = 60_000;
const OPEN = 1;
const enc = new TextEncoder();
const dec = new TextDecoder();

function frame(kind, id, payload) {
  const out = new Uint8Array(1 + ID_LEN + payload.byteLength);
  out[0] = kind;
  out.set(enc.encode(id), 1);
  out.set(payload, 1 + ID_LEN);
  return out;
}

const codeBytes = (code) => new Uint8Array([(code >> 8) & 255, code & 255]);

// Codes that may legally be sent in a close frame
function sendableCode(code) {
  return code === 1000 || (code >= 1001 && code <= 1014 && ![1004, 1005, 1006].includes(code)) ||
    (code >= 3000 && code <= 4999) ? code : 1000;
}

function sameSecret(a, b) {
  const x = enc.encode(a), y = enc.encode(b);
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}

async function sha256Hex(text) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(text)));
  return [...d].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if ((req.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return new Response("ACC relay is running", { status: 200 });
    }
    if (url.pathname !== "/hub" && url.pathname !== "/ws/device") {
      return new Response("Not found", { status: 404 });
    }
    const stub = env.RELAY.get(env.RELAY.idFromName("main"));
    return stub.fetch(req);
  },
};

export class Relay extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("hb", "hb"));
  }

  // The live laptop connection; a hub that stopped sending heartbeats is dropped.
  hub() {
    for (const ws of this.ctx.getWebSockets("hub")) {
      if (ws.readyState !== OPEN) continue;
      const since = ws.deserializeAttachment()?.since || 0;
      const last = this.ctx.getWebSocketAutoResponseTimestamp(ws)?.getTime() || since;
      if (Date.now() - last > HUB_TIMEOUT_MS) {
        try { ws.close(4008, "laptop heartbeat lost"); } catch {}
        continue;
      }
      return ws;
    }
    return undefined;
  }

  devices() {
    return this.ctx.getWebSockets().filter((ws) => ws.readyState === OPEN && ws.deserializeAttachment()?.role === "dev");
  }

  device(id) {
    return this.ctx.getWebSockets("dev:" + id).find((ws) => ws.readyState === OPEN);
  }

  dropDevices(code, reason) {
    for (const d of this.devices()) try { d.close(code, reason); } catch {}
  }

  async fetch(req) {
    const url = new URL(req.url);

    if (url.pathname === "/hub") {
      const key = req.headers.get("X-Hub-Key") || "";
      if (!this.env.HUB_KEY || !sameSecret(key, this.env.HUB_KEY)) {
        return new Response("Forbidden", { status: 403 });
      }
      const [client, server] = Object.values(new WebSocketPair());
      // Only one laptop at a time; a reconnecting laptop has lost its per-phone state,
      // so phones are asked to reconnect (they do so automatically).
      for (const old of this.ctx.getWebSockets("hub")) try { old.close(4000, "replaced by another laptop session"); } catch {}
      this.dropDevices(1012, "laptop reconnected");
      this.ctx.acceptWebSocket(server, ["hub"]);
      server.serializeAttachment({ role: "hub", since: Date.now() });
      await this.ctx.storage.setAlarm(Date.now() + HUB_TIMEOUT_MS);
      return new Response(null, { status: 101, webSocket: client });
    }

    // Phone: authenticate before upgrading
    const hub = this.hub();
    if (!hub) return new Response("Laptop is offline", { status: 503 });
    const token = req.headers.get("X-ACC-Token") || url.searchParams.get("token") || "";
    const hashes = (await this.ctx.storage.get("hashes")) || [];
    if (!token || !hashes.includes(await sha256Hex(token))) {
      return new Response("Unauthorized", { status: 401 });
    }
    if (this.devices().length >= MAX_DEVICES) return new Response("Too many connections", { status: 503 });

    const [client, server] = Object.values(new WebSocketPair());
    const id = crypto.randomUUID().replace(/-/g, "").slice(0, ID_LEN);
    this.ctx.acceptWebSocket(server, ["dev:" + id]);
    server.serializeAttachment({ role: "dev", id });
    const ip = req.headers.get("CF-Connecting-IP") || "0.0.0.0";
    hub.send(frame(K_OPEN, id, enc.encode(JSON.stringify({ token, ip }))));
    return new Response(null, { status: 101, webSocket: client });
  }

  // Periodic liveness check while a laptop is attached: if it vanished without closing,
  // release the phones so they fail over to their other addresses.
  async alarm() {
    if (this.hub()) {
      await this.ctx.storage.setAlarm(Date.now() + HUB_TIMEOUT_MS);
    } else {
      this.dropDevices(1012, "laptop offline");
    }
  }

  async webSocketMessage(ws, msg) {
    const att = ws.deserializeAttachment() || {};
    if (att.role === "hub") {
      if (typeof msg === "string") return;
      const buf = new Uint8Array(msg);
      if (buf.byteLength < 1 + ID_LEN) return;
      const kind = buf[0];
      const id = dec.decode(buf.subarray(1, 1 + ID_LEN));
      const body = buf.subarray(1 + ID_LEN);
      if (kind === K_AUTH) {
        try {
          const hashes = JSON.parse(dec.decode(body)).hashes;
          if (Array.isArray(hashes)) await this.ctx.storage.put("hashes", hashes.filter((h) => typeof h === "string"));
        } catch {}
        return;
      }
      const dev = this.device(id);
      if (!dev) {
        if (kind !== K_CLOSE) ws.send(frame(K_CLOSE, id, codeBytes(1001)));
        return;
      }
      try {
        if (kind === K_TEXT) dev.send(dec.decode(body));
        else if (kind === K_BIN) dev.send(body);
        else if (kind === K_CLOSE) {
          const code = body.byteLength >= 2 ? (body[0] << 8) | body[1] : 1000;
          dev.close(sendableCode(code), "closed by laptop");
        }
      } catch {}
      return;
    }
    if (att.role === "dev") {
      const size = typeof msg === "string" ? msg.length * 3 : msg.byteLength;
      if (size > MAX_DEVICE_MESSAGE) {
        try { ws.close(1009, "message too large"); } catch {}
        return;
      }
      const hub = this.hub();
      if (!hub) {
        try { ws.close(1012, "laptop offline"); } catch {}
        return;
      }
      if (typeof msg === "string") hub.send(frame(K_TEXT, att.id, enc.encode(msg)));
      else hub.send(frame(K_BIN, att.id, new Uint8Array(msg)));
    }
  }

  async webSocketClose(ws, code, reason) {
    const att = ws.deserializeAttachment() || {};
    if (att.role === "dev") {
      try { this.hub()?.send(frame(K_CLOSE, att.id, codeBytes(code || 1000))); } catch {}
    } else if (att.role === "hub") {
      const otherHub = this.ctx.getWebSockets("hub").some((h) => h !== ws && h.readyState === OPEN);
      if (!otherHub) this.dropDevices(1012, "laptop offline");
    }
    try { ws.close(sendableCode(code), reason); } catch {}
  }

  async webSocketError(ws) {
    await this.webSocketClose(ws, 1011, "error");
  }
}
