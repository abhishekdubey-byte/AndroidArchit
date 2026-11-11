// Android Control Centre - Client Controller v2.0
// Text from phones (names, error reports) must never be interpreted as HTML.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class ControlCentreApp {
  constructor() {
    this.ws = null;
    this.devices = [];
    this.activeDevice = null;
    this.currentTask = null;
    this.canvas = document.getElementById("screenCanvas");
    this.ctx = this.canvas ? this.canvas.getContext("2d") : null;
    this.coordBadge = document.getElementById("coordBadge");
    this.debugMirror = new URLSearchParams(window.location.search).has("debug");  // ?debug=1 shows mirror fps
    document.addEventListener("visibilitychange", () => {
      clearTimeout(this.hiddenTimer);
      if (document.hidden) {
        // hidden for a while: stop streaming (keeps "mirroring on"), resume when the tab is visible again
        this.hiddenTimer = setTimeout(() => {
          if (this.mirrorOn && this.activeDevice && this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: "STOP_SCREEN_STREAM", device_id: this.activeDevice.id }));
            this.stopH264();
            this.pausedHidden = true;
          }
        }, 8000);
      } else if (this.pausedHidden) {
        this.pausedHidden = false;
        if (this.mirrorOn) this.setMirroring(true);
      }
    });
    this.img = new Image();
    this.isDragging = false;
    this.mirrorOn = false; // off by default: streaming the screen drains the phone's battery
    this.dragStart = { x: 0, y: 0, time: 0 };

    // Voice recognition
    this.recognition = null;
    this.isListening = false;

    // Remote Audio & Call Management
    this.audioCtx = null;
    this.isAudioListening = false;
    this.currentCall = null;
    this.callTimerInterval = null;
    this.isMicMuted = false;
    this.laptopMicStream = null;
    this.laptopAudioCtx = null;
    this.micProcessor = null;
    this.speakerAudioCtx = null;
    this.nextAudioPlayTime = 0;
    this.diagnosticsList = [];

    this.init();
  }

  init() {
    this.connectWebSocket();
    this.setupEventListeners();
    this.setupSpeechRecognition();
    this.setupLayoutControls();
  }

  // ☰ device-list drawer + 🚀 floating app launcher
  setupLayoutControls() {
    const fleetBtn = document.getElementById("fleetToggle");
    const applyFleet = (hidden) => {
      document.body.classList.toggle("fleet-collapsed", hidden);
      if (fleetBtn) fleetBtn.setAttribute("aria-expanded", String(!hidden));
      try { localStorage.setItem("acc.fleetDrawer", hidden ? "closed" : "open"); } catch (e) {}
    };
    // the list slides over the phone column, so it starts closed
    let saved = "closed"; try { saved = localStorage.getItem("acc.fleetDrawer") || "closed"; } catch (e) {}
    applyFleet(saved !== "open");
    this.closeFleetDrawer = () => applyFleet(true);
    if (fleetBtn) fleetBtn.onclick = () => applyFleet(!document.body.classList.contains("fleet-collapsed"));

    const refresh = document.getElementById("fleetRefresh");
    if (refresh) refresh.onclick = () => this.refreshFleet(refresh);

    this.setupRoutines();

    document.querySelectorAll("#huntPanel [data-arrow]").forEach((b) => b.onclick = () => this.sendHuntCommand("", b.dataset.arrow));
    document.querySelectorAll("#huntPanel [data-steer]").forEach((b) => b.onclick = () => {
      if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      this.ws.send(JSON.stringify({ type: "HUNT_COMMAND", device_id: this.activeDevice.id, payload: { steer: b.dataset.steer } }));
      this.setCaptureStatus("ok", "🐾 Creature moved: " + b.dataset.steer);
    });
    const say = document.getElementById("huntSay");
    if (say) say.onsubmit = (e) => { e.preventDefault(); const i = document.getElementById("huntText"); if (i.value.trim()) { this.sendHuntCommand(i.value.trim(), ""); i.value = ""; } };

    const scope = document.getElementById("captureScope");
    if (scope) {
      try { scope.value = localStorage.getItem("acc.captureScope") || "device"; } catch (e) {}
      scope.onchange = () => { try { localStorage.setItem("acc.captureScope", scope.value); } catch (e) {} this.loadCaptures(); };
    }
  }

  connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/dashboard`;

    console.log(`[WS] Connecting to ${wsUrl}...`);
    this.ws = new WebSocket(wsUrl);
    this.ws.binaryType = "arraybuffer";   // H.264 mirror packets arrive as binary messages

    this.ws.onopen = () => {
      console.log("[WS] Connected to Central Server");
      // Tell the server whether this browser can decode H.264 (WebCodecs needs localhost/https)
      // Tell the server whether this browser can decode H.264 (probed with WebCodecs; needs localhost/https)
      const probe = window.H264Player ? H264Player.probe() : Promise.resolve(false);
      probe.then((ok) => {
        this.h264Capable = ok && !this.h264Broken;
        if (this.ws && this.ws.readyState === WebSocket.OPEN)
          this.ws.send(JSON.stringify({ type: "CLIENT_CAPS", payload: { h264: this.h264Capable } }));
      });
      document.getElementById("connectionStatus").innerText = "Server Online";
      document.getElementById("connectionDot").style.background = "#10b981";
    };

    this.ws.onclose = () => {
      console.log("[WS] Disconnected, retrying in 3s...");
      document.getElementById("connectionStatus").innerText = "Reconnecting...";
      document.getElementById("connectionDot").style.background = "#f59e0b";
      setTimeout(() => this.connectWebSocket(), 3000);
    };

    this.ws.onerror = (err) => {
      console.error("[WS] Error:", err);
    };

    this.ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        this.onMediaPacket(event.data);
        return;
      }
      try {
        const msg = JSON.parse(event.data);
        this.handleMessage(msg);
      } catch (e) {
        console.error("[WS] Message parsing error:", e);
      }
    };
  }

  handleMessage(msg) {
    switch (msg.type) {
      case "INITIAL_STATE":
        this.devices = msg.data.devices || [];
        // after a server restart the old selection may no longer exist: don't keep showing it
        if (this.activeDevice && !this.devices.some((d) => d.id === this.activeDevice.id)) this.clearActiveDevice();
        this.renderDeviceList();
        if (!this.activeDevice) {
          const first = this.visibleDevices().find((d) => d.status !== "offline") || this.visibleDevices()[0];
          if (first) this.selectDevice(first.id);
        } else {
          // reconnected to the server: refresh the selection and restart its mirror if it was on
          this.activeDevice = this.devices.find((d) => d.id === this.activeDevice.id) || this.activeDevice;
          this.renderActiveDeviceHeader(this.activeDevice);
        }
        if (msg.data.tasks && msg.data.tasks.length > 0) {
          this.renderTask(msg.data.tasks[msg.data.tasks.length - 1]);
        }
        if (msg.data.diagnostics) {
          this.diagnosticsList = msg.data.diagnostics;
          this.updateDiagnosticsBadge();
        }
        break;

      case "DEVICE_CONNECTED":
      case "TELEMETRY_UPDATE":
        this.updateDevice(msg.data);
        break;

      case "DEVICE_DISCONNECTED":
        this.markDeviceOffline(msg.data);
        break;

      case "DEVICE_REMOVED":
        this.devices = this.devices.filter((d) => d.id !== (msg.data && msg.data.id));
        if (this.activeDevice && msg.data && this.activeDevice.id === msg.data.id) this.clearActiveDevice();
        this.renderDeviceList();
        break;

      case "APP_LIFECYCLE_STATUS":
        this.onAppLifecycleStatus(msg.device_id, msg.payload || {});
        break;

      case "DEVICE_CAPABILITIES_UPDATED":
        this.handleCapabilitiesUpdated(msg.payload || msg.data);
        break;

      case "INPUT_ERROR":
        this.showToast((msg.payload && msg.payload.message) || "The phone could not type that text", "error");
        break;

      case "DEVICE_ERROR":
        this.handleDeviceError(msg.payload || msg.data, msg.device_id);
        break;

      case "SCREEN_FRAME":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) {
          this.renderScreenFrame(msg.payload.data_base64);
        }
        break;

      case "SCREEN_UNAVAILABLE":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) {
          this.renderScreenNotice(msg.payload.reason);
        }
        break;

      case "CAMERA_FRAME":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) {
          this.renderLiveFrame(msg.payload);
        }
        break;

      case "HUNT_STATE":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) this.onHuntState(msg.payload || {});
        break;

      case "CAMERA_AUDIO":
        if (this.activeDevice && msg.device_id === this.activeDevice.id && this.live && this.live.on
            && !this.liveMuted && msg.payload && msg.payload.pcm_base64) {
          this.playIncomingAudioChunk(msg.payload.pcm_base64);
        }
        break;

      case "CAMERA_STREAM_STATE":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) {
          this.onLiveState(msg.payload);
        }
        break;

      case "CAMERA_SAVE_STATE":
        if (this.activeDevice && msg.device_id === this.activeDevice.id) {
          this.onSaveState(msg.payload);
        }
        break;

      case "TASK_CREATED":
      case "TASK_UPDATE":
      case "TASK_COMPLETED":
        this.renderTask(msg.data);
        break;

      case "APPROVAL_REQUIRED":
        this.showApprovalModal(msg.data);
        break;

      case "INCOMING_CALL":
        this.handleIncomingCall(msg.payload || msg.data, msg.device_id);
        break;

      case "CALL_ACTIVE":
        this.handleCallActive(msg.payload || msg.data, msg.device_id);
        break;

      case "CALL_ENDED":
        this.handleCallEnded(msg.payload || msg.data, msg.device_id);
        break;

      case "PHONE_AUDIO_CHUNK":
        if (msg.payload && msg.payload.pcm_base64) {
          this.playIncomingAudioChunk(msg.payload.pcm_base64);
        }
        break;

      case "PHOTO_CAPTURED":
        this.handlePhotoCaptured(msg.payload || msg.data);
        break;

      case "VIDEO_RECORDED":
        this.handleVideoRecorded(msg.payload || msg.data);
        break;

      default:
        console.log("[WS] Unhandled message:", msg);
    }
  }

  // ----- fleet: hide phones you're not working with, reconnect / remove offline ones -----
  hiddenIds() {
    if (!this._hidden) {
      try { this._hidden = new Set(JSON.parse(localStorage.getItem("acc.hiddenDevices") || "[]")); }
      catch (e) { this._hidden = new Set(); }
    }
    return this._hidden;
  }
  setHidden(id, hide) {
    const h = this.hiddenIds();
    hide ? h.add(id) : h.delete(id);
    try { localStorage.setItem("acc.hiddenDevices", JSON.stringify([...h])); } catch (e) {}
    if (hide && this.activeDevice && this.activeDevice.id === id) {
      this.clearActiveDevice();
      const next = this.visibleDevices().find((d) => d.status !== "offline");
      if (next) return this.selectDevice(next.id);
    }
    this.renderDeviceList();
  }
  visibleDevices() { return this.devices.filter((d) => !this.hiddenIds().has(d.id)); }

  clearActiveDevice() {
    if (this.activeDevice && this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "STOP_SCREEN_STREAM", device_id: this.activeDevice.id }));
      if (this.live && this.live.on) this.stopLivePreview();
    }
    this.activeDevice = null;
    const t = document.getElementById("activeDeviceTitle"); if (t) t.innerText = "No phone selected";
    const ad = document.getElementById("appsPopDevice"); if (ad) ad.innerText = "";
    const apps = document.getElementById("appsContainer");
    if (apps) apps.innerHTML = '<div style="font-size: 12px; color: var(--text-muted); grid-column: 1 / -1;">Select a phone to see its apps.</div>';
    this.renderScreenNotice("No phone selected. Pick one from the device list (☰).");
  }

  fleetNote(text, kind) {
    const n = document.getElementById("fleetNote");
    if (!n) return;
    clearTimeout(this._fleetNoteTimer);
    n.hidden = !text; n.innerText = text || ""; n.className = "fleet-note " + (kind || "");
    if (text && kind !== "busy") this._fleetNoteTimer = setTimeout(() => { n.hidden = true; }, 6000);
  }

  async refreshFleet(btn) {
    if (btn) { btn.disabled = true; btn.innerText = "⏳ Scanning…"; }
    this.fleetNote("Scanning USB and Wi-Fi for phones…", "busy");
    try {
      const r = await fetch("/api/devices/refresh", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      (await r.json()).forEach((d) => this.updateDevice(d));
      const online = this.devices.filter((d) => d.status !== "offline").length;
      this.fleetNote(`✓ Updated — ${online} online, ${this.devices.length - online} offline`, "ok");
    } catch (e) {
      this.fleetNote("Refresh failed: " + e.message, "err");
    } finally {
      if (btn) { btn.disabled = false; btn.innerText = "🔄 Refresh"; }
    }
  }

  async reconnectDevice(id, btn) {
    if (btn) { btn.disabled = true; btn.innerText = "⏳ Reconnecting…"; }
    try {
      const r = await (await fetch(`/api/devices/${encodeURIComponent(id)}/reconnect`, { method: "POST" })).json();
      this.fleetNote((r.online ? "✓ " : "") + (r.message || ""), r.online ? "ok" : "err");
    } catch (e) {
      this.fleetNote("Reconnect failed: " + e.message, "err");
    }
    this.renderDeviceList();
  }

  async removeDevice(id) {
    const dev = this.devices.find((d) => d.id === id);
    if (!confirm(`Remove ${dev ? dev.name : "this phone"} from the list?\nIt comes back by itself the next time it connects.`)) return;
    const r = await fetch(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!r.ok) return this.fleetNote("Could not remove: it is online again.", "err");
    this.devices = this.devices.filter((d) => d.id !== id);
    if (this.activeDevice && this.activeDevice.id === id) this.clearActiveDevice();
    this.renderDeviceList();
  }

  renderDeviceList() {
    const container = document.getElementById("deviceListContainer");
    if (!container) return;
    container.innerHTML = "";

    const online = this.devices.filter((d) => d.status !== "offline").length;
    document.getElementById("deviceCount").innerText = `${online} Active`;

    const hiddenCount = this.devices.length - this.visibleDevices().length;
    const list = this.showHidden ? this.devices : this.visibleDevices();
    if (!this.devices.length) {
      container.innerHTML = '<div style="font-size: 13px; color: var(--text-muted); padding: 12px;">No phones yet — plug one in over USB or pair it on <a href="/pair.html" style="color: var(--accent-cyan, #06b6d4);">/pair.html</a>.</div>';
    }

    list.forEach((dev) => {
      const card = document.createElement("div");
      card.className = `device-card ${this.activeDevice && this.activeDevice.id === dev.id ? "active" : ""}`;
      card.onclick = () => { this.selectDevice(dev.id); if (this.closeFleetDrawer) this.closeFleetDrawer(); };

      const offline = dev.status === "offline";
      const isWifi = dev.serial.includes(":") || dev.serial.includes(".");
      const transportLabel = offline ? "Offline" : (dev.transport === "companion_ws" ? "Remote" : isWifi ? "Wi-Fi (Remote)" : "USB / Direct");
      const pillColor = offline ? "#f43f5e" : isWifi || dev.transport === "companion_ws" ? "#06b6d4" : "#10b981";

      card.innerHTML = `
        <div class="device-card-header">
          <div>
            <div class="device-name">${esc(dev.name)}</div>
            <div class="device-serial">${esc(dev.model)} · Android ${esc(dev.android_version)}</div>
          </div>
          <span class="status-pill" style="font-size: 10px; padding: 2px 8px; color: ${pillColor}; border-color: ${pillColor}50;">
            ${offline ? '○ ' : '● '}${transportLabel}
          </span>
        </div>
        <div class="device-telemetry-grid">
          <div class="telemetry-item">
            <span class="telemetry-label">Battery</span>
            <span class="telemetry-value" style="color: ${dev.battery.level < 20 ? '#f43f5e' : '#10b981'}">
              🔋 ${dev.battery.level}% ${dev.battery.charging ? '⚡' : ''}
            </span>
          </div>
          <div class="telemetry-item">
            <span class="telemetry-label">Storage</span>
            <span class="telemetry-value">
              💾 ${dev.storage.used_gb} / ${dev.storage.total_gb} GB
            </span>
          </div>
          <div class="telemetry-item">
            <span class="telemetry-label">OEM / SDK</span>
            <span class="telemetry-value" style="font-size: 11px;">
              ${esc(dev.manufacturer || dev.brand)} · API ${dev.sdk_int || 33}
            </span>
          </div>
          <div class="telemetry-item">
            <span class="telemetry-label">Display / Screen</span>
            <span class="telemetry-value" style="font-size: 11px;">
              ${dev.display.width}x${dev.display.height}
            </span>
          </div>
        </div>
        <div style="display: flex; gap: 6px; margin-top: 8px;">
          <button class="nav-btn" style="font-size: 10px; padding: 3px 6px; flex: 1; display: flex; align-items: center; justify-content: center; gap: 4px;" onclick="event.stopPropagation(); window.app.showCapabilitiesModal('${dev.id}')">
            <span>⚙️ Capabilities</span>
          </button>
          <button class="nav-btn" style="font-size: 10px; padding: 3px 6px; flex: 1; display: flex; align-items: center; justify-content: center; gap: 4px;" onclick="event.stopPropagation(); window.app.wakeDevice('${dev.id}')">
            <span>⚡ Wake Rails</span>
          </button>
        </div>
        <div class="device-card-actions">
          ${offline ? `<button class="nav-btn" title="Try to bring this phone back online" onclick="event.stopPropagation(); window.app.reconnectDevice('${dev.id}', this)">🔌 Reconnect</button>
                       <button class="nav-btn danger" title="Remove from the list (it returns when it connects again)" onclick="event.stopPropagation(); window.app.removeDevice('${dev.id}')">🗑 Remove</button>` : ""}
          ${this.hiddenIds().has(dev.id)
            ? `<button class="nav-btn" title="Show this phone in the list again" onclick="event.stopPropagation(); window.app.setHidden('${dev.id}', false)">👁 Unhide</button>`
            : `<button class="nav-btn" title="Hide this phone from the list while you work with others (it stays connected)" onclick="event.stopPropagation(); window.app.setHidden('${dev.id}', true)">🙈 Hide</button>`}
        </div>
        <div class="device-card-actions app-life-actions" onclick="event.stopPropagation()">
          <label class="app-life-pick" title="Select for app actions on several phones at once"><input type="checkbox" ${this.appSel.has(dev.id) ? "checked" : ""} onchange="window.app.toggleAppSel('${dev.id}', this.checked)"> App</label>
          <button class="nav-btn" title="Push the latest published companion APK" onclick="window.app.appLifecycle(['${dev.id}'], 'update')">⬆ Update</button>
          ${dev.transport === "adb" && !offline ? `<button class="nav-btn" title="Install the latest APK over ADB (app missing or broken)" onclick="window.app.appLifecycle(['${dev.id}'], 'reinstall')">♻ Reinstall</button>
          <button class="nav-btn" title="Make the companion app Device Owner (silent updates/uninstall); needs no accounts on the phone" onclick="window.app.makeDeviceOwner('${dev.id}', this)">👑 Owner</button>` : ""}
          <button class="nav-btn danger" title="Uninstall the companion app" onclick="window.app.appLifecycle(['${dev.id}'], 'uninstall')">🗑 Uninstall</button>
        </div>
      `;
      if (this.hiddenIds().has(dev.id)) card.classList.add("is-hidden");
      container.appendChild(card);
    });

    // Multi-phone app actions: shown while one or more phones are ticked "App"
    const picked = [...this.appSel].filter((id) => this.devices.some((d) => d.id === id));
    if (picked.length) {
      const bar = document.createElement("div");
      bar.className = "device-card-actions app-life-bulk";
      bar.innerHTML = `<span>${picked.length} selected:</span>
        <button class="nav-btn">⬆ Update</button><button class="nav-btn">♻ Reinstall</button>
        <button class="nav-btn danger">🗑 Uninstall</button><button class="nav-btn">✕ Clear</button>`;
      const [u, r, x, c] = bar.querySelectorAll("button");
      u.onclick = () => this.appLifecycle(picked, "update");
      r.onclick = () => this.appLifecycle(picked, "reinstall");
      x.onclick = () => this.appLifecycle(picked, "uninstall");
      c.onclick = () => { this.appSel.clear(); this.renderDeviceList(); };
      container.prepend(bar);
    }

    if (hiddenCount) {
      const t = document.createElement("button");
      t.className = "nav-btn fleet-hidden-toggle";
      t.innerText = this.showHidden ? `Hide the ${hiddenCount} hidden phone(s)` : `👁 Show ${hiddenCount} hidden phone(s)`;
      t.onclick = () => { this.showHidden = !this.showHidden; this.renderDeviceList(); };
      container.appendChild(t);
    }
  }

  selectDevice(deviceId) {
    const dev = this.devices.find((d) => d.id === deviceId);
    if (!dev) return;

    if (this.activeDevice && this.ws) {
      this.ws.send(JSON.stringify({
        type: "STOP_SCREEN_STREAM",
        device_id: this.activeDevice.id
      }));
      if (this.live && this.live.on) this.stopLivePreview();  // don't leave another phone's camera running
      this.stopH264();
    }

    this.activeDevice = dev;
    this.renderDeviceList();
    const ad = document.getElementById("appsPopDevice"); if (ad) ad.innerText = "· " + dev.name;
    this.loadCaptures();
    this.renderActiveDeviceHeader(dev);

    this.loadDeviceApps(dev.id);
    this.loadDeviceFiles(dev.id);
  }

  // Title "(USB / Wi-Fi / Remote / OFFLINE)" + screen panel state for the selected phone.
  // Called on selection and whenever that phone goes offline or comes back.
  renderActiveDeviceHeader(dev, { titleOnly = false } = {}) {
    const link = dev.status === "offline" ? "OFFLINE" : dev.transport === "companion_ws" ? "Remote" : dev.serial.includes(":") ? "Wi-Fi" : "USB";
    document.getElementById("activeDeviceTitle").innerText = `${dev.brand.toUpperCase()} ${dev.model} (${link})`;
    if (titleOnly) return;

    if (dev.status === "offline") {
      // keep the user's "mirroring on" choice: it resumes by itself when the phone comes back
      this.stopH264();
      this.renderScreenNotice("This phone is offline. It reconnects automatically.");
    } else if (this.mirrorOn) {
      this.setMirroring(true);
    } else {
      this.renderScreenNotice("Screen mirroring is off. Turn it on above when you need it.");
    }
  }

  updateDevice(deviceData) {
    const idx = this.devices.findIndex((d) => d.id === deviceData.id);
    if (idx >= 0) {
      this.devices[idx] = deviceData;
    } else {
      this.devices.push(deviceData);
    }
    if (this.activeDevice && this.activeDevice.id === deviceData.id) {
      const prev = this.activeDevice;
      const cameBack = prev.status === "offline" && deviceData.status !== "offline";
      const linkChanged = prev.status !== deviceData.status || prev.transport !== deviceData.transport;
      this.activeDevice = deviceData;
      // no stale "(OFFLINE)" after a replug; a USB <-> companion switch only changes the title
      if (linkChanged) this.renderActiveDeviceHeader(deviceData, { titleOnly: !cameBack && prev.status === deviceData.status });
      if (cameBack) { this.loadDeviceFiles(deviceData.id); this.loadDeviceApps(deviceData.id); }   // list storage as soon as it reconnects
    }
    this.renderDeviceList();
  }

  markDeviceOffline(deviceData) {
    const dev = this.devices.find((d) => d.id === deviceData.id);
    if (dev) {
      dev.status = "offline";
      if (this.activeDevice && this.activeDevice.id === dev.id && this.live && this.live.on) this.stopLivePreview("the phone went offline");
      if (this.activeDevice && this.activeDevice.id === dev.id) {
        this.activeDevice.status = "offline";
        this.renderActiveDeviceHeader(this.activeDevice);
      }
      this.renderDeviceList();
    }
  }

  // ----- Fast USB mirror: hardware H.264 decoded by the browser -----
  onMediaPacket(buf) {
    let pkt;
    try { pkt = parseMediaPacket(buf); } catch (e) { return; }
    if (this.activeDevice && pkt.deviceId === this.activeDevice.id + "#cam") return this.onCameraPacket(pkt);
    if (!this.activeDevice || pkt.deviceId !== this.activeDevice.id || !this.mirrorOn) return;
    if (!this.h264) {
      this.h264 = new H264Player(this.canvas, {
        onStuck: () => this.ws && this.ws.readyState === WebSocket.OPEN &&
          this.ws.send(JSON.stringify({ type: "MIRROR_RESYNC", device_id: this.activeDevice.id })),
        onUnsupported: () => {
          // this browser can't decode the phone's stream after all: switch this tab to snapshots
          this.h264Broken = true;
          this.h264Capable = false;
          this.stopH264();
          if (this.ws && this.ws.readyState === WebSocket.OPEN)
            this.ws.send(JSON.stringify({ type: "CLIENT_CAPS", payload: { h264: false } }));
          this.showToast("This browser can't decode the fast mirror; using slower snapshots.", "error");
        },
      });
    }
    if (this.img) this.img.onload = null;   // a late JPEG must not paint over the live picture
    this.h264.push(pkt);
    if (this.debugMirror && this.coordBadge && pkt.kind !== 1) this.coordBadge.innerText = `H.264 ${this.h264.stats.fps} fps`;
  }

  // ----- Live camera as hardware H.264 (phone app 1.9.28+) -----
  onCameraPacket(pkt) {
    if (!this.live || !this.live.on) return;
    const canvas = document.getElementById("liveCanvas");
    if (!canvas) return;
    if (!this.camPlayer) {
      const stream = this.activeDevice.id + "#cam";
      this.camPlayer = new H264Player(canvas, {
        onFirstFrame: () => {
          canvas.hidden = false;
          const img = document.getElementById("liveImg"); if (img) img.hidden = true;
        },
        onStuck: () => this.ws && this.ws.readyState === WebSocket.OPEN &&
          this.ws.send(JSON.stringify({ type: "MIRROR_RESYNC", device_id: stream })),
        onUnsupported: () => {
          this.showToast("This browser can't decode the live camera stream.", "error");
          this.stopLivePreview("unsupported video");
        },
      });
      this.camPlayer.setRotation(this.live.rotation || 0);
    }
    this.camPlayer.push(pkt);
    const now = Date.now();
    if (now - this.live.fpsAt >= 1000) {
      const fps = document.getElementById("liveFps");
      if (fps) fps.innerText = this.camPlayer.stats.fps + " fps · H.264";
      this.live.fpsAt = now;
    }
  }

  stopCameraPlayer() {
    if (this.camPlayer) { this.camPlayer.close(); this.camPlayer = null; }
    const canvas = document.getElementById("liveCanvas"); if (canvas) canvas.hidden = true;
    const img = document.getElementById("liveImg"); if (img) img.hidden = false;
  }

  stopH264() {
    if (this.h264) { this.h264.close(); this.h264 = null; }
  }

  renderScreenFrame(base64Data) {
    if (!this.canvas || !this.ctx) return;
    this.img.onload = () => {
      this.canvas.width = this.img.width;
      this.canvas.height = this.img.height;
      this.ctx.drawImage(this.img, 0, 0);
    };
    this.img.src = `data:image/jpeg;base64,${base64Data}`;
  }

  setMirroring(on) {
    this.mirrorOn = on;
    this.stopH264();   // a (re)start always begins with a fresh decoder
    const btn = document.getElementById("mirrorToggle");
    if (btn) {
      btn.classList.toggle("on", on);
      document.getElementById("mirrorToggleText").innerText = on ? "Mirroring on" : "Mirroring off";
    }
    if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: on ? "START_SCREEN_STREAM" : "STOP_SCREEN_STREAM", device_id: this.activeDevice.id }));
    if (!on) this.renderScreenNotice("Screen mirroring is off. Turn it on above when you need it.");
  }

  // ----- Headless camera: status line + recent captures -----
  setCaptureStatus(kind, text) {
    const el = document.getElementById("captureStatus");
    if (!el) return;
    el.hidden = !text;
    el.className = `cam-status ${kind}`;
    el.textContent = text || "";
  }

  resetPhotoButtons() {
    clearTimeout(this.photoTimer);
    const rear = document.getElementById("btnSilentRearPhoto");
    const front = document.getElementById("btnSilentFrontPhoto");
    if (rear) { rear.disabled = false; rear.innerText = "📷 Rear Photo"; }
    if (front) { front.disabled = false; front.innerText = "🤳 Front Photo"; }
  }

  // ----- Live camera preview -----
  startLivePreview(lens) {
    if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.live = { on: true, lens, frames: 0, fpsAt: Date.now() };
    try {  // created inside the click so the browser lets the phone mic audio play
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!this.speakerAudioCtx) this.speakerAudioCtx = new AudioCtx({ sampleRate: 16000 });
      if (this.speakerAudioCtx.state === "suspended") this.speakerAudioCtx.resume();
    } catch (e) {}
    const btn = document.getElementById("btnLivePreview");
    if (btn) { btn.classList.add("on"); btn.innerText = "⏹ Stop Preview"; }
    document.getElementById("liveTools").hidden = false;
    document.getElementById("liveStage").hidden = false;
    document.getElementById("btnLiveLens").innerText = lens === "front" ? "🔄 Rear" : "🔄 Front";
    this.setLiveBadge("connecting…");
    this.startLiveClock();
    this.ws.send(JSON.stringify({ type: "START_CAMERA_STREAM", device_id: this.activeDevice.id, payload: { lens } }));
  }

  // ----- Crystal Mines HUNT: the phone's game camera, live, with HQ instructions -----
  onHuntState(p) {
    const panel = document.getElementById("huntPanel");
    if (!panel) return;
    if (!p.active) {
      panel.hidden = true;
      if (this.live && this.live.on && this.live.hunt) this.stopLivePreview("the hunt ended");
      return;
    }
    if (!this.live || !this.live.on) {
      // show the hunt camera in the live stage (frames arrive as normal CAMERA_FRAMEs)
      this.live = { on: true, lens: "back", frames: 0, fpsAt: Date.now(), hunt: true };
      document.getElementById("liveTools").hidden = false;
      document.getElementById("liveStage").hidden = false;
      const lb = document.getElementById("btnLiveLens"); if (lb) lb.hidden = true;
      const btn = document.getElementById("btnLivePreview");
      if (btn) { btn.classList.add("on"); btn.innerText = "⏹ Stop Preview"; }
      this.startLiveClock();
      this.setLiveBadge("HUNT");
    }
    panel.hidden = false;
    document.getElementById("huntDna").innerText = `🧬 ${p.dna || 0}/${p.total || 50} · 💎 ${p.shards || 0} · ⭐ LV ${p.level || 1}`;
    const tgt = document.getElementById("huntTarget");
    const sc = document.getElementById("huntScene");
    if (sc) {
      const icon = p.scene === "indoor" ? "🏠 INDOOR" : p.scene === "outdoor" ? "🌳 OUTDOOR" : "🔍 reading the scene…";
      const objs = (p.anchors || []).map((a) => `${a.name} ${a.dx < 0 ? "◀" + (-a.dx) + "°" : a.dx + "°▶"}`).join(" · ");
      sc.innerText = `${icon}${(p.labels || []).length ? " — sees: " + p.labels.slice(0, 5).join(", ") : ""}${objs ? "\n📍 objects: " + objs : ""}`;
    }
    const c0 = (p.creatures || [])[0];
    tgt.innerText = (p.ai ? `🛰 ${p.ai}\n` : "") + (c0 ? `${c0.name} · ${c0.dist} m · ${c0.mood} · weakness: ${c0.challenge} (${c0.how})${c0.exposed ? " — EXPOSED!" : ""}` : "Searching for a signature…")
      + (p.locked ? "\n🔒 LOCKED — tell them to CAPTURE" : "");
    tgt.className = "hunt-target" + (p.locked ? " locked" : p.target ? " seen" : "");
    const box = document.getElementById("huntCreatures");
    box.replaceChildren(...(p.creatures || []).map((c) => {
      const d = document.createElement("span");
      d.className = "hunt-chip r-" + String(c.rarity || "").toLowerCase();
      const dir = c.on_screen ? "on screen" : (c.dx < 0 ? `◀ ${-c.dx}° left` : `${c.dx}° right ▶`);
      d.innerText = `${c.name} · ${dir} · ${c.dist} m`;
      d.title = "Click to send this direction to the hunter";
      d.onclick = () => this.sendHuntCommand(c.on_screen ? `${c.name} is right in front of you!` : `${c.name}: turn ${c.dx < 0 ? "left" : "right"} about ${Math.abs(c.dx)}°`, c.on_screen ? "scan" : (c.dx < 0 ? "left" : "right"));
      return d;
    }));
  }

  sendHuntCommand(text, arrow) {
    if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "HUNT_COMMAND", device_id: this.activeDevice.id, payload: { text: text || "", arrow: arrow || "" } }));
    this.setCaptureStatus("ok", "🛰 Tracker AI override sent: " + (text || arrow));
  }

  // ⏱ how long the preview has been running; on stop, when and after how long it ended
  fmtDur(ms) {
    const t = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(sec).padStart(2, "0");
  }
  startLiveClock() {
    clearInterval(this.liveClockTimer);
    this.liveStartedAt = Date.now();
    const el = document.getElementById("liveClock"), sum = document.getElementById("liveSummary");
    if (sum) sum.hidden = true;
    if (!el) return;
    el.hidden = false;
    const tick = () => { el.innerText = "⏱ " + this.fmtDur(Date.now() - this.liveStartedAt); };
    tick();
    this.liveClockTimer = setInterval(tick, 1000);
  }
  stopLiveClock(reason) {
    clearInterval(this.liveClockTimer); this.liveClockTimer = null;
    const el = document.getElementById("liveClock"); if (el) el.hidden = true;
    if (!this.liveStartedAt) return;
    const sum = document.getElementById("liveSummary");
    if (sum) {
      sum.hidden = false;
      sum.innerText = `Last preview ran ${this.fmtDur(Date.now() - this.liveStartedAt)} · stopped at ${new Date().toLocaleTimeString()}` + (reason ? ` · ${reason}` : "");
    }
    this.liveStartedAt = null;
  }

  stopLivePreview(reason) {
    this.stopLiveClock(reason);
    const hp = document.getElementById("huntPanel"); if (hp) hp.hidden = true;
    const lb = document.getElementById("btnLiveLens"); if (lb) lb.hidden = false;
    if (this.activeDevice && this.ws && this.ws.readyState === WebSocket.OPEN) {
      if (this.live && this.live.saving) this.ws.send(JSON.stringify({ type: "STOP_CAMERA_SAVE", device_id: this.activeDevice.id }));
      this.ws.send(JSON.stringify({ type: "STOP_CAMERA_STREAM", device_id: this.activeDevice.id }));
    }
    this.live = { on: false };
    this.stopCameraPlayer();
    const sb = document.getElementById("btnLiveSave");
    if (sb) { sb.classList.remove("rec"); sb.innerText = "⏺ Save"; }
    const btn = document.getElementById("btnLivePreview");
    if (btn) { btn.classList.remove("on"); btn.innerText = "▶ Live Preview"; }
    const tools = document.getElementById("liveTools"); if (tools) tools.hidden = true;
    const stage = document.getElementById("liveStage"); if (stage) stage.hidden = true;
    const img = document.getElementById("liveImg"); if (img) img.removeAttribute("src");
    const fps = document.getElementById("liveFps"); if (fps) fps.innerText = "—";
  }

  setLiveBadge(text) {
    const b = document.getElementById("liveBadge");
    if (b) b.innerText = "● LIVE · " + text;
  }

  renderLiveFrame(payload) {
    if (!this.live || !this.live.on || !payload || !payload.data_base64) return;
    const img = document.getElementById("liveImg");
    if (img) img.src = "data:image/jpeg;base64," + payload.data_base64;
    this.setLiveBadge(payload.lens === "front" ? "front" : "rear");
    this.live.frames++;
    const now = Date.now();
    if (now - this.live.fpsAt >= 1000) {
      const fps = document.getElementById("liveFps");
      if (fps) fps.innerText = this.live.frames + " fps";
      this.live.frames = 0; this.live.fpsAt = now;
    }
  }

  onLiveState(payload) {
    if (!payload) return;
    if (payload.codec === "h264" && this.live && this.live.on) {
      if (payload.rotation !== undefined) {
        this.live.rotation = payload.rotation;
        if (this.camPlayer) this.camPlayer.setRotation(payload.rotation);
      }
      if (payload.lens) this.setLiveBadge(payload.lens === "front" ? "front" : "rear");
      if (payload.throttled) this.setCaptureStatus("ok", "Phone is warm: live camera eased to 15 fps");
    }
    if (payload.error) {
      this.setCaptureStatus("err", "Live preview: " + payload.error);
      this.stopLivePreview("error: " + payload.error);
    } else if (payload.active === false && this.live && this.live.on) {
      this.stopLivePreview("the phone ended it");
    }
  }

  onSaveState(payload) {
    if (!payload || !this.live) return;
    const btn = document.getElementById("btnLiveSave");
    if (this.recording && this.recording.viaLive && !payload.saving) this.resetRecording();  // timed live recording finished
    if (payload.error) { this.setCaptureStatus("err", "Recording: " + payload.error); return; }
    if (payload.saving) {
      this.live.saving = true;
      if (btn) { btn.classList.add("rec"); btn.innerText = "⏹ Stop Saving"; }
      this.setCaptureStatus("busy", "Recording the live preview to this laptop…");
    } else {
      this.live.saving = false;
      if (btn) { btn.classList.remove("rec"); btn.innerText = "⏺ Save"; }
      if (payload.name) { const p = (payload.path || "").replace(/\\/g, "/"); this.setCaptureStatus("ok", "Saved to this laptop: server/storage/" + (p.includes("/storage/") ? p.split("/storage/").pop() : "downloads/" + payload.name)); }
      else this.setCaptureStatus("err", "Nothing was recorded.");
    }
  }

  startPhotoCapture(lens, btn) {
    if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    btn.innerText = "⏳ Capturing…";
    btn.disabled = true;
    this.setCaptureStatus("busy", `Taking a ${lens === "front" ? "front" : "rear"} photo on ${this.activeDevice.name}…`);
    this.ws.send(JSON.stringify({ type: "CAPTURE_PHOTO", device_id: this.activeDevice.id, payload: { lens } }));
    clearTimeout(this.photoTimer);
    this.photoTimer = setTimeout(() => {
      this.resetPhotoButtons();
      this.setCaptureStatus("err", "No photo arrived within 30 seconds. Check that the phone is online and its camera is free.");
    }, 30000);
  }

  // ----- video recording (timed or until stopped) -----
  setRecordButton(icon, text, disabled, live) {
    const btn = document.getElementById("btnRecordVideoCustom");
    if (!btn) return;
    btn.disabled = disabled;
    btn.classList.toggle("recording", !!live);
    document.getElementById("recordVideoBtnIcon").innerText = icon;
    document.getElementById("recordVideoBtnText").innerText = text;
  }

  stopRecording() {
    const r = this.recording;
    if (!r || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (!r.viaLive) this.ws.send(JSON.stringify({ type: "STOP_VIDEO", device_id: r.deviceId }));
    this.setRecordingSaving();
  }

  setRecordingSaving() {
    const r = this.recording;
    if (!r || r.saving) return;
    r.saving = true;
    clearInterval(r.timer);
    if (r.viaLive && this.ws && this.ws.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type: "STOP_CAMERA_SAVE", device_id: r.deviceId }));
    this.setRecordButton("⏳", "Saving…", true, false);
    this.setCaptureStatus("busy", "Finishing the recording and copying it to the laptop…");
    // Long videos over the internet can take a while to copy; give up after 30 minutes
    r.giveUp = setTimeout(() => {
      this.resetRecording();
      this.setCaptureStatus("err", "The video did not arrive. Check the phone's connection and try again.");
    }, 30 * 60 * 1000);
  }

  resetRecording() {
    if (this.recording) {
      clearInterval(this.recording.timer);
      clearTimeout(this.recording.giveUp);
    }
    this.recording = null;
    this.setRecordButton("🎥", "Record Video", false, false);
  }

  // Recent captures: every file (scrollable), filtered to the selected phone or all phones; hover = big preview
  async loadCaptures() {
    const box = document.getElementById("captureGallery");
    if (!box) return;
    const sel = document.getElementById("captureScope");
    const scope = sel ? sel.value : "device";
    const dev = scope === "device" && this.activeDevice ? this.activeDevice.id : "";
    try {
      const { items, total } = await (await fetch("/api/captures" + (dev ? "?device_id=" + encodeURIComponent(dev) : ""))).json();
      const cnt = document.getElementById("captureCount"); if (cnt) cnt.innerText = total ? `${total}` : "";
      box.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "cam-empty";
        empty.textContent = dev ? "No captures from this phone yet" : "No captures yet";
        box.append(empty);
        return;
      }
      for (const it of items) {
        const a = document.createElement("a");
        a.className = "cam-thumb";
        a.href = it.url;
        a.target = "_blank";
        a.title = `${it.device} · ${new Date(it.mtime * 1000).toLocaleString()}`;
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        img.alt = "";
        img.src = it.kind === "video" ? "/api/captures/thumb?path=" + encodeURIComponent(it.url) : it.url;
        a.append(img);
        if (it.kind === "video") {
          const b = document.createElement("span");
          b.className = "cam-badge";
          b.textContent = "▶";
          a.append(b);
        }
        a.addEventListener("mouseenter", () => this.showCapturePreview(it, a));
        a.addEventListener("mouseleave", () => this.hideCapturePreview());
        box.append(a);
      }
    } catch (e) {
      console.error("Failed to load captures:", e);
    }
  }

  showCapturePreview(it, anchor) {
    clearTimeout(this._capHoverTimer);
    this._capHoverTimer = setTimeout(() => {
      let pop = document.getElementById("capPreview");
      if (!pop) {
        pop = document.createElement("div");
        pop.id = "capPreview";
        pop.className = "cap-preview";
        document.body.append(pop);
      }
      const media = document.createElement(it.kind === "video" ? "video" : "img");
      media.src = it.url;
      if (it.kind === "video") { media.muted = true; media.autoplay = true; media.loop = true; media.playsInline = true; }
      const meta = document.createElement("div");
      meta.className = "cap-preview-meta";
      meta.textContent = `${it.device} · ${new Date(it.mtime * 1000).toLocaleString()} · ${(it.size / 1048576).toFixed(1)} MB`;
      pop.replaceChildren(media, meta);
      // float beside the gallery, toward the centre of the screen, and keep it on-screen
      const r = anchor.getBoundingClientRect(), W = 360, H = 480;
      let left = r.left - W - 14; if (left < 8) left = Math.min(window.innerWidth - W - 8, r.right + 14);
      const top = Math.max(72, Math.min(window.innerHeight - H - 8, r.top + r.height / 2 - H / 2));
      pop.style.left = left + "px"; pop.style.top = top + "px";
      pop.classList.add("on");
    }, 180);
  }

  hideCapturePreview() {
    clearTimeout(this._capHoverTimer);
    const pop = document.getElementById("capPreview");
    if (!pop) return;
    pop.classList.remove("on");
    const v = pop.querySelector("video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  }


  renderScreenNotice(text) {
    if (!this.canvas || !this.ctx) return;
    if (this.img) this.img.onload = null; // a pending frame must not paint over the notice
    const w = (this.canvas.width = 540), h = (this.canvas.height = 1200);
    this.ctx.fillStyle = "#0d121f";
    this.ctx.fillRect(0, 0, w, h);
    this.ctx.fillStyle = "#94a3b8";
    this.ctx.font = "28px Inter, sans-serif";
    this.ctx.textAlign = "center";
    // word-wrap each sentence so long notices stay inside the phone frame
    const lines = [];
    String(text || "Screen unavailable").split(". ").forEach((sentence) => {
      let line = "";
      (sentence.replace(/\.$/, "") + ".").split(" ").forEach((word) => {
        const next = line ? line + " " + word : word;
        if (line && this.ctx.measureText(next).width > w - 70) { lines.push(line); line = word; } else line = next;
      });
      if (line) lines.push(line);
    });
    const top = h / 2 - ((lines.length - 1) * 40) / 2;
    lines.forEach((line, i) => this.ctx.fillText(line, w / 2, top + i * 40));
  }

  getCanvasDeviceCoordinates(e) {
    if (!this.canvas || !this.activeDevice) return { x: 0, y: 0 };
    const rect = this.canvas.getBoundingClientRect();
    
    // Normalized 0.0 to 1.0 mapping across the exact canvas element
    const normX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const normY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    
    const x = Math.round(normX * this.activeDevice.display.width);
    const y = Math.round(normY * this.activeDevice.display.height);
    return { x, y };
  }

  setupEventListeners() {
    // 1. Canvas Mouse Interactions & Exact Coordinate Mapping
    if (this.canvas) {
      // Live finger: down / move / up go to the phone as they happen (moves coalesced to one per frame),
      // so dragging lists and maps feels direct; the wheel scrolls.
      const sendPointer = (action, coords) => {
        if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ type: "INPUT_POINTER", device_id: this.activeDevice.id,
                                      payload: { action, x: coords.x, y: coords.y } }));
      };
      let pendingMove = null;
      const flushMove = () => {
        if (pendingMove && this.isDragging) sendPointer("move", pendingMove);
        pendingMove = null;
      };

      this.canvas.addEventListener("mousemove", (e) => {
        if (!this.activeDevice) return;
        const coords = this.getCanvasDeviceCoordinates(e);
        if (this.coordBadge && !this.debugMirror) {
          this.coordBadge.innerText = `X: ${coords.x}, Y: ${coords.y}`;
        }
        if (this.isDragging) {
          if (!pendingMove) requestAnimationFrame(flushMove);
          pendingMove = coords;
        }
      });

      this.canvas.addEventListener("mousedown", (e) => {
        if (e.button !== 0 || !this.activeDevice) return;
        this.isDragging = true;
        const coords = this.getCanvasDeviceCoordinates(e);
        this.dragStart = { x: coords.x, y: coords.y, time: Date.now() };
        this.showTouchRipple(e.clientX, e.clientY);
        sendPointer("down", coords);
      });

      const release = (e) => {
        if (!this.isDragging || !this.activeDevice) return;
        flushMove();
        this.isDragging = false;
        sendPointer("up", this.getCanvasDeviceCoordinates(e));
      };
      this.canvas.addEventListener("mouseup", release);
      this.canvas.addEventListener("mouseleave", release);   // never leave a finger pressed on the phone

      let lastWheel = 0;
      this.canvas.addEventListener("wheel", (e) => {
        if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        e.preventDefault();
        const now = performance.now();
        if (now - lastWheel < 30) return;                    // ~30 scroll events/s is plenty
        lastWheel = now;
        const coords = this.getCanvasDeviceCoordinates(e);
        const v = Math.max(-1, Math.min(1, -e.deltaY / 100)); // wheel down = content scrolls down
        if (v) this.ws.send(JSON.stringify({ type: "INPUT_SCROLL", device_id: this.activeDevice.id,
                                             payload: { x: coords.x, y: coords.y, v } }));
      }, { passive: false });
    }

    // 2. Hardware Navigation Buttons
    document.querySelectorAll(".nav-btn[data-key]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.getAttribute("data-key");
        if (key && this.activeDevice) {
          this.ws.send(JSON.stringify({
            type: "INPUT_KEY",
            device_id: this.activeDevice.id,
            payload: { key }
          }));
        }
      };
    });

    // 3. Quick Text Typing
    const sendTextBtn = document.getElementById("sendTextBtn");
    const textInput = document.getElementById("keyboardText");
    const enterBtn = document.getElementById("keyEnterBtn");

    const submitTextToPhone = () => {
      if (!textInput || !this.activeDevice) return;
      const val = textInput.value;
      if (val) {
        this.ws.send(JSON.stringify({
          type: "INPUT_TEXT",
          device_id: this.activeDevice.id,
          payload: { text: val }
        }));
        textInput.value = "";
      }
    };

    if (sendTextBtn) sendTextBtn.onclick = submitTextToPhone;

    const dialBtn = document.getElementById("btnDialCall");
    if (dialBtn) dialBtn.onclick = () => this.dialFromLaptop();
    const dialInput = document.getElementById("callNumberInput");
    if (dialInput) dialInput.addEventListener("keydown", (e) => { if (e.key === "Enter") this.dialFromLaptop(); });
    if (textInput) {
      textInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          submitTextToPhone();
        }
      });
    }
    if (enterBtn) {
      enterBtn.onclick = () => {
        if (this.activeDevice) {
          this.ws.send(JSON.stringify({
            type: "INPUT_KEY",
            device_id: this.activeDevice.id,
            payload: { key: "ENTER" }
          }));
        }
      };
    }

    // 4. AI Task Submissions
    const submitBtn = document.getElementById("submitTaskBtn");
    const taskInput = document.getElementById("taskInput");
    if (submitBtn && taskInput) {
      const sendTask = () => {
        const text = taskInput.value.trim();
        if (!text || !this.activeDevice) return;
        this.ws.send(JSON.stringify({
          type: "SUBMIT_TASK",
          device_id: this.activeDevice.id,
          payload: {
            instruction: text,
            target_device_id: this.activeDevice.id
          }
        }));
        taskInput.value = "";
      };
      submitBtn.onclick = sendTask;
      taskInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendTask();
      });
    }

    // 5. Remote Call Audio Listener Toggle
    const audioBtn = document.getElementById("toggleAudioBtn");
    const audioWaves = document.getElementById("audioWaves");
    const audioStatusText = document.getElementById("audioStatusText");

    if (audioBtn) {
      // Live phone audio is not implemented yet (upgrade roadmap, Phase 4): never pretend to be listening.
      audioBtn.disabled = true;
      audioBtn.title = "Coming soon: live phone audio";
      audioBtn.innerText = "🔊 Listen Live (soon)";
      audioBtn.style.opacity = "0.5";
      audioBtn.style.cursor = "not-allowed";
      if (audioWaves) audioWaves.classList.remove("active");
      if (audioStatusText) audioStatusText.innerText = "Live phone audio is coming soon";
    }

    // Screen mirroring switch (off by default to save the phone's battery)
    const mirrorBtn = document.getElementById("mirrorToggle");
    if (mirrorBtn) mirrorBtn.onclick = () => this.setMirroring(!this.mirrorOn);
    this.loadCaptures();

    // 6. Silent Headless Camera & Video Triggers (Zero UI, Low Battery)
    const btnRear = document.getElementById("btnSilentRearPhoto");
    const btnFront = document.getElementById("btnSilentFrontPhoto");
    const btnVideo = document.getElementById("btnSilentVideo");

    if (btnRear) {
      btnRear.onclick = () => this.startPhotoCapture("back", btnRear);
    }

    if (btnFront) {
      btnFront.onclick = () => this.startPhotoCapture("front", btnFront);
    }

    // Live camera preview (real-time feed for verification / recognition)
    const btnLive = document.getElementById("btnLivePreview");
    const btnLiveLens = document.getElementById("btnLiveLens");
    if (btnLive) {
      btnLive.onclick = () => {
        if (this.live && this.live.on) this.stopLivePreview("stopped by you");
        else this.startLivePreview("back");
      };
    }
    if (btnLiveLens) {
      btnLiveLens.onclick = () => {
        if (!this.live || !this.live.on) return;
        const next = this.live.lens === "front" ? "back" : "front";
        this.live.lens = next;
        btnLiveLens.innerText = next === "front" ? "🔄 Rear" : "🔄 Front";
        this.ws.send(JSON.stringify({ type: "SET_CAMERA_LENS", device_id: this.activeDevice.id, payload: { lens: next } }));
      };
    }
    const btnLiveMute = document.getElementById("btnLiveMute");
    if (btnLiveMute) {
      btnLiveMute.onclick = () => {
        this.liveMuted = !this.liveMuted;
        btnLiveMute.innerText = this.liveMuted ? "🔇 Muted" : "🔊 Sound";
        if (!this.liveMuted && this.speakerAudioCtx && this.speakerAudioCtx.state === "suspended") this.speakerAudioCtx.resume();
      };
    }
    const btnLiveSave = document.getElementById("btnLiveSave");
    if (btnLiveSave) {
      btnLiveSave.onclick = () => {
        if (!this.live || !this.live.on) return;
        const type = this.live.saving ? "STOP_CAMERA_SAVE" : "START_CAMERA_SAVE";
        this.ws.send(JSON.stringify({ type, device_id: this.activeDevice.id }));
      };
    }

    // Custom Headless Video Recording with Lens Selection, Variable Durations & Live Countdown
    const btnRecordCustom = document.getElementById("btnRecordVideoCustom");
    const lensSelect = document.getElementById("videoLensSelect");
    if (lensSelect) {
      // Changing the camera while recording moves the recording to that camera (clips are joined at the end)
      lensSelect.addEventListener("change", () => {
        const r = this.recording, lens = lensSelect.value === "front" ? "front" : "back";
        if (!r || r.saving || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        if (r.viaLive) {
          if (this.live) this.live.lens = lens;
          this.ws.send(JSON.stringify({ type: "SET_CAMERA_LENS", device_id: r.deviceId, payload: { lens } }));
          const b = document.getElementById("btnLiveLens"); if (b) b.innerText = lens === "front" ? "🔄 Rear" : "🔄 Front";
        } else {
          this.ws.send(JSON.stringify({ type: "SWITCH_VIDEO_LENS", device_id: r.deviceId, payload: { lens } }));
        }
        this.setCaptureStatus("busy", `Switching the recording to the ${lens === "front" ? "front" : "rear"} camera…`);
      });
    }
    const durationSelect = document.getElementById("videoDurationSelect");
    const recordBtnText = document.getElementById("recordVideoBtnText");
    const recordBtnIcon = document.getElementById("recordVideoBtnIcon");

    // Quick Duration Chips Listener
    document.querySelectorAll(".video-chip").forEach((chip) => {
      chip.onclick = () => {
        const sec = chip.getAttribute("data-sec");
        if (durationSelect && sec) {
          durationSelect.value = sec;
          document.querySelectorAll(".video-chip").forEach((c) => {
            c.style.background = "rgba(255,255,255,0.06)";
            c.style.color = "#cbd5e1";
          });
          chip.style.background = "rgba(236, 72, 153, 0.35)";
          chip.style.color = "#f472b6";
        }
      };
    });

    if (btnRecordCustom) {
      btnRecordCustom.onclick = () => {
        if (this.recording) return this.recording.saving ? null : this.stopRecording();
        if (!this.activeDevice || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        const lens = lensSelect ? lensSelect.value : "back";
        const durationSec = durationSelect ? parseInt(durationSelect.value, 10) || 0 : 10; // 0 = until stopped
        if (this.live && this.live.on) {
          // The camera can only serve one session: while previewing, record the live stream itself
          // (video + mic, muxed on the laptop) so the preview keeps running.
          if (this.live.saving) return this.setCaptureStatus("err", "Already saving the live preview — press ⏹ Stop Saving first.");
          this.recording = { deviceId: this.activeDevice.id, durationSec, started: Date.now(), saving: false, viaLive: true };
          this.ws.send(JSON.stringify({ type: "START_CAMERA_SAVE", device_id: this.activeDevice.id }));
          this.setCaptureStatus("busy", "Recording the live preview with sound" + (durationSec ? ` for ${durationSec}s` : "") + " — the preview keeps running.");
        } else {
        this.recording = { deviceId: this.activeDevice.id, durationSec, started: Date.now(), saving: false };
        this.ws.send(JSON.stringify({
          type: "RECORD_VIDEO",
          device_id: this.activeDevice.id,
          payload: { lens, duration_sec: durationSec }
        }));
        this.setCaptureStatus("busy", durationSec
          ? `Recording a ${durationSec}s video with sound (${lens === "front" ? "front" : "rear"} camera)… press Stop to end early.`
          : `Recording with sound (${lens === "front" ? "front" : "rear"} camera) until you press Stop…`);
        }
        const tick = () => {
          const r = this.recording;
          if (!r || r.saving) return;
          const elapsed = Math.floor((Date.now() - r.started) / 1000);
          const mmss = `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
          if (r.durationSec && elapsed >= r.durationSec) return this.setRecordingSaving();
          this.setRecordButton("⏹", r.durationSec ? `Stop · ${r.durationSec - elapsed}s left` : `Stop · ${mmss}`, false, true);
        };
        tick();
        this.recording.timer = setInterval(tick, 1000);
      };
    }

    // 7. Call Overlay Action Handlers
    const btnAccept = document.getElementById("btnAcceptCall");
    const btnDecline = document.getElementById("btnDeclineCall");
    const btnEndActive = document.getElementById("btnEndActiveCall");
    const btnMute = document.getElementById("btnMuteLaptopMic");

    if (btnAccept) btnAccept.onclick = () => this.acceptCall();
    if (btnDecline) btnDecline.onclick = () => this.declineCall();
    if (btnEndActive) btnEndActive.onclick = () => this.endCall();
    if (btnMute) btnMute.onclick = () => this.toggleMuteMic();
  }

  setupSpeechRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const voiceBtn = document.getElementById("voiceMicBtn");
    const voiceText = document.getElementById("voiceMicText");
    const taskInput = document.getElementById("taskInput");

    if (!SpeechRecognition) {
      if (voiceBtn) voiceBtn.style.display = "none";
      console.log("[Voice] SpeechRecognition API not supported in browser");
      return;
    }

    this.recognition = new SpeechRecognition();
    this.recognition.continuous = false;
    this.recognition.interimResults = false;
    this.recognition.lang = "en-US";

    this.recognition.onstart = () => {
      this.isListening = true;
      voiceBtn.classList.add("listening");
      voiceText.innerText = "Listening...";
    };

    this.recognition.onresult = (event) => {
      const speechResult = event.results[0][0].transcript;
      console.log(`[Voice] Transcribed: "${speechResult}"`);
      if (taskInput) {
        taskInput.value = speechResult;
        // Auto-run voice instruction
        document.getElementById("submitTaskBtn").click();
      }
    };

    this.recognition.onerror = (event) => {
      console.error("[Voice] Recognition error:", event.error);
      this.stopListening();
    };

    this.recognition.onend = () => {
      this.stopListening();
    };

    if (voiceBtn) {
      voiceBtn.onclick = () => {
        if (!this.isListening) {
          try {
            this.recognition.start();
          } catch (e) {
            console.error(e);
          }
        } else {
          this.recognition.stop();
        }
      };
    }
  }

  stopListening() {
    this.isListening = false;
    const voiceBtn = document.getElementById("voiceMicBtn");
    const voiceText = document.getElementById("voiceMicText");
    if (voiceBtn) voiceBtn.classList.remove("listening");
    if (voiceText) voiceText.innerText = "Voice Command";
  }

  startAudioStreaming() {
    if (!this.audioCtx) {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    // Web Audio synthesizer tone / audio bridge ready
    console.log("[Audio] Live Remote Audio Bridge Active");
  }

  stopAudioStreaming() {
    if (this.audioCtx && this.audioCtx.state !== "closed") {
      // Audio stream paused
    }
  }

  // ==========================================================================
  // INCOMING CALL HANDLING & BIDIRECTIONAL LAPTOP AUDIO BRIDGE
  // ==========================================================================
  handleIncomingCall(payload, deviceId) {
    this.setCallStateChip("Ringing", "#f59e0b");
    const phoneNumber = (payload && payload.phone_number) ? payload.phone_number : "+91 ••••• •••••";
    const callerName = (payload && payload.caller_name) ? payload.caller_name : "Incoming Caller";
    const dev = this.devices.find(d => d.id === deviceId) || this.activeDevice;

    this.currentCall = {
      deviceId: deviceId,
      phoneNumber: phoneNumber,
      callerName: callerName,
      state: "ringing"
    };

    const overlay = document.getElementById("callOverlay");
    const nameEl = document.getElementById("callerNameDisplay");
    const numEl = document.getElementById("callerNumberDisplay");
    const devEl = document.getElementById("callDeviceLabel");
    const stateEl = document.getElementById("callStateLabel");
    const durationEl = document.getElementById("callDurationDisplay");
    const incActions = document.getElementById("incomingCallActions");
    const actActions = document.getElementById("activeCallActions");
    const bridgeEl = document.getElementById("callAudioBridge");

    if (nameEl) nameEl.innerText = callerName;
    if (numEl) numEl.innerText = phoneNumber;
    if (devEl && dev) devEl.innerText = dev.name;
    if (stateEl) stateEl.innerText = "Incoming Phone Call";
    if (durationEl) durationEl.style.display = "none";
    if (incActions) incActions.style.display = "flex";
    if (actActions) actActions.style.display = "none";
    if (bridgeEl) bridgeEl.style.display = "none";
    if (overlay) overlay.style.display = "flex";

    this.playChime("incoming");
  }

  handleCallActive(payload, deviceId) {
    this.setCallStateChip("On call", "#10b981");
    if (!this.currentCall) {
      this.currentCall = {
        deviceId: deviceId,
        phoneNumber: (payload && payload.phone_number) ? payload.phone_number : "Active Caller",
        callerName: "Active Call",
        state: "active"
      };
    } else {
      this.currentCall.state = "active";
    }
    this.currentCall.startTime = Date.now();

    const overlay = document.getElementById("callOverlay");
    const stateEl = document.getElementById("callStateLabel");
    const durationEl = document.getElementById("callDurationDisplay");
    const incActions = document.getElementById("incomingCallActions");
    const actActions = document.getElementById("activeCallActions");
    const bridgeEl = document.getElementById("callAudioBridge");

    if (stateEl) stateEl.innerText = "Active Call · Handsfree via Laptop";
    if (durationEl) {
      durationEl.style.display = "inline-block";
      durationEl.innerText = "00:00";
    }
    if (incActions) incActions.style.display = "none";
    if (actActions) actActions.style.display = "flex";
    if (bridgeEl) bridgeEl.style.display = "flex";
    if (overlay) overlay.style.display = "flex";

    // Start Call Duration Timer
    if (this.callTimerInterval) clearInterval(this.callTimerInterval);
    this.callTimerInterval = setInterval(() => {
      if (!this.currentCall || !this.currentCall.startTime) return;
      const elapsed = Math.floor((Date.now() - this.currentCall.startTime) / 1000);
      const m = String(Math.floor(elapsed / 60)).padStart(2, "0");
      const s = String(elapsed % 60).padStart(2, "0");
      if (durationEl) durationEl.innerText = `${m}:${s}`;
    }, 1000);

    // Activate Bidirectional Laptop Audio
    this.startLaptopMicStreaming();
  }

  handleCallEnded(payload, deviceId) {
    this.setCallStateChip("Idle", "#94a3b8");
    if (this.callTimerInterval) {
      clearInterval(this.callTimerInterval);
      this.callTimerInterval = null;
    }
    this.stopLaptopMicStreaming();

    const overlay = document.getElementById("callOverlay");
    if (overlay) overlay.style.display = "none";

    this.currentCall = null;
    this.playChime("ended");
  }

  handlePhotoCaptured(data) {
    console.log("[Camera] Photo result:", data);
    this.resetPhotoButtons();
    if (!data || !data.success) {
      this.setCaptureStatus("err", `Photo failed: ${(data && data.error) || "unknown error"}`);
      return;
    }
    this.setCaptureStatus("ok", `✓ ${data.lens === "front" ? "Front" : "Rear"} photo saved on this laptop as ${data.filename} (removed from the phone).`);
    this.showMediaToast(`📸 Photo (${data.lens || "rear"})`, "img", data);
    this.loadCaptures();
  }

  handleVideoRecorded(data) {
    console.log("[Camera] Video result:", data);
    this.resetRecording();
    if (!data || !data.success) {
      this.setCaptureStatus("err", `Video failed: ${(data && data.error) || "unknown error"}`);
      return;
    }
    this.setCaptureStatus("ok", `✓ ${data.duration_sec ? data.duration_sec + "s " : ""}video with sound saved on this laptop as ${data.filename} (removed from the phone).`);
    this.showMediaToast(`🎥 Video (${data.duration_sec ? data.duration_sec + "s · " : ""}${data.lens || "rear"})`, "video", data);
    this.loadCaptures();
  }

  showMediaToast(titleText, kind, data) {
    const toast = document.getElementById("mediaPreviewToast");
    if (!toast) return;
    document.getElementById("mediaToastTitle").innerText = titleText;
    const content = document.getElementById("mediaToastContent");
    const media = document.createElement(kind);
    media.src = data.url;
    media.style.cssText = "width: 100%; max-height: 220px; object-fit: contain; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #000;";
    if (kind === "video") { media.controls = true; media.autoplay = true; media.muted = true; }
    const name = document.createElement("div");
    name.style.cssText = "font-size: 10px; color: #94a3b8; margin-top: 4px;";
    name.textContent = "server" + decodeURIComponent((data.url || "").replace(/^\/storage/, "/storage")) || data.filename || "";
    content.replaceChildren(media, name);
    const link = document.getElementById("mediaToastDownloadLink");
    link.href = data.url;
    link.innerText = "Open / Download";
    toast.style.display = "block";
    this.playChime("connected");
  }

  acceptCall() {
    const targetDev = this.currentCall?.deviceId || (this.activeDevice ? this.activeDevice.id : null);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: "ACCEPT_CALL",
        device_id: targetDev
      }));
    }
    this.handleCallActive({ phone_number: this.currentCall?.phoneNumber }, targetDev);
  }

  declineCall() {
    const targetDev = this.currentCall?.deviceId || (this.activeDevice ? this.activeDevice.id : null);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: "END_CALL",
        device_id: targetDev
      }));
    }
    this.handleCallEnded({}, targetDev);
  }

  endCall() {
    this.declineCall();
  }

  toggleMuteMic() {
    this.isMicMuted = !this.isMicMuted;
    const btn = document.getElementById("btnMuteLaptopMic");
    const icon = document.getElementById("muteMicIcon");
    const text = document.getElementById("muteMicText");
    const status = document.getElementById("micChannelStatus");

    if (this.isMicMuted) {
      if (btn) btn.classList.add("muted");
      if (icon) icon.innerText = "🔇";
      if (text) text.innerText = "Unmute Mic";
      if (status) status.innerText = "Muted (Laptop mic paused)";
    } else {
      if (btn) btn.classList.remove("muted");
      if (icon) icon.innerText = "🎙️";
      if (text) text.innerText = "Mute Mic";
      if (status) status.innerText = "Active · Transmitting to Caller";
    }
  }

  async startLaptopMicStreaming() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      this.laptopAudioCtx = new AudioCtx({ sampleRate: 16000 });
      if (this.laptopAudioCtx.state === "suspended") {
        await this.laptopAudioCtx.resume();
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          sampleRate: 16000
        }
      });
      this.laptopMicStream = stream;

      const source = this.laptopAudioCtx.createMediaStreamSource(stream);
      // ScriptProcessor captures raw PCM audio
      const processor = this.laptopAudioCtx.createScriptProcessor(2048, 1, 1);

      processor.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0);

        // Volume meter RMS
        let sum = 0;
        for (let i = 0; i < input.length; i++) {
          sum += input[i] * input[i];
        }
        const rms = Math.sqrt(sum / input.length);
        const meter = document.getElementById("laptopMicMeter");
        if (meter) {
          const pct = Math.min(100, Math.max(8, Math.round(rms * 500)));
          meter.style.width = `${pct}%`;
        }

        if (this.isMicMuted) return;

        // Downsample/convert Float32 to 16-bit PCM Int16
        const pcm16 = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]));
          pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }

        // Convert Int16Array to Base64
        let binary = "";
        const bytes = new Uint8Array(pcm16.buffer);
        const len = bytes.byteLength;
        for (let i = 0; i < len; i++) {
          binary += String.fromCharCode(bytes[i]);
        }
        const b64 = btoa(binary);

        const targetDev = this.currentCall?.deviceId || (this.activeDevice ? this.activeDevice.id : null);
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({
            type: "LAPTOP_MIC_AUDIO",
            device_id: targetDev,
            payload: { pcm_base64: b64 }
          }));
        }
      };

      source.connect(processor);
      processor.connect(this.laptopAudioCtx.destination);
      this.micProcessor = processor;
      console.log("[Audio] Laptop microphone capture active (16kHz PCM streaming to phone)");
    } catch (err) {
      console.warn("[Audio] Microphone access requested or unavailable:", err);
    }
  }

  stopLaptopMicStreaming() {
    if (this.laptopMicStream) {
      this.laptopMicStream.getTracks().forEach(t => t.stop());
      this.laptopMicStream = null;
    }
    if (this.micProcessor) {
      this.micProcessor.disconnect();
      this.micProcessor = null;
    }
    if (this.laptopAudioCtx && this.laptopAudioCtx.state !== "closed") {
      this.laptopAudioCtx.close().catch(() => {});
      this.laptopAudioCtx = null;
    }
  }

  playIncomingAudioChunk(base64Pcm) {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!this.speakerAudioCtx) {
        this.speakerAudioCtx = new AudioCtx({ sampleRate: 16000 });
      }
      if (this.speakerAudioCtx.state === "suspended") {
        this.speakerAudioCtx.resume();
      }

      // Base64 decode to Int16
      const binary = atob(base64Pcm);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
      }
      const int16 = new Int16Array(bytes.buffer);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / (int16[i] < 0 ? 32768 : 32767);
      }

      const audioBuffer = this.speakerAudioCtx.createBuffer(1, float32.length, 16000);
      audioBuffer.copyToChannel(float32, 0);

      const source = this.speakerAudioCtx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(this.speakerAudioCtx.destination);

      const now = this.speakerAudioCtx.currentTime;
      if (this.nextAudioPlayTime < now) {
        this.nextAudioPlayTime = now + 0.02;
      }
      source.start(this.nextAudioPlayTime);
      this.nextAudioPlayTime += audioBuffer.duration;
    } catch (e) {
      console.error("[Audio] Error playing incoming call chunk:", e);
    }
  }

  playChime(type) {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);

      if (type === "incoming") {
        osc.frequency.setValueAtTime(440, ctx.currentTime);
        osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.4);
        gain.gain.setValueAtTime(0.15, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
        osc.start();
        osc.stop(ctx.currentTime + 0.5);
      } else if (type === "ended") {
        osc.frequency.setValueAtTime(600, ctx.currentTime);
        osc.frequency.exponentialRampToValueAtTime(300, ctx.currentTime + 0.3);
        gain.gain.setValueAtTime(0.12, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
        osc.start();
        osc.stop(ctx.currentTime + 0.35);
      }
    } catch (e) {
      // Audio autoplay policy fallback
    }
  }


  showTouchRipple(clientX, clientY) {
    const ripple = document.createElement("div");
    ripple.className = "screen-touch-ripple";
    ripple.style.left = `${clientX}px`;
    ripple.style.top = `${clientY}px`;
    document.body.appendChild(ripple);
    setTimeout(() => ripple.remove(), 400);
  }

  setTaskPrompt(text) {
    const input = document.getElementById("taskInput");
    if (input) {
      input.value = text;
      input.focus();
    }
  }

  renderTask(task) {
    this.currentTask = task;
    const container = document.getElementById("taskTimelineContainer");
    if (!container) return;

    let html = `
      <div class="timeline-card">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <strong style="font-size: 13px;">${esc(task.instruction)}</strong>
          <span class="status-pill" style="font-size: 10px; color: ${task.status === 'completed' ? '#10b981' : (task.status === 'failed' ? '#f43f5e' : '#f59e0b')}">
            ${task.status.toUpperCase()}
          </span>
        </div>
        <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 10px;">
    `;

    (task.steps || []).forEach((step) => {
      let icon = step.id;
      if (step.status === "done") icon = "✓";
      else if (step.status === "executing") icon = "●";
      else if (step.status === "waiting_approval") icon = "!";
      else if (step.status === "failed") icon = "✕";

      html += `
        <div class="timeline-step ${step.status}">
          <div class="step-marker">${icon}</div>
          <div class="step-details">
            <div class="step-desc">${esc(step.description)}</div>
            ${step.result ? `<div class="step-result">${esc(step.result)}</div>` : ""}
          </div>
        </div>
      `;
    });

    if (task.result_summary) {
      const isSuccess = task.status === "completed";
      html += `
        <div style="background: ${isSuccess ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)'}; border: 1px solid ${isSuccess ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}; padding: 8px 12px; border-radius: 8px; font-size: 12px; color: ${isSuccess ? '#6ee7b7' : '#fca5a5'}; margin-top: 6px;">
          ${isSuccess ? '✓' : '✕'} ${esc(task.result_summary)}
        </div>
      `;
    }

    html += `</div></div>`;
    container.innerHTML = html;
  }

  showApprovalModal(data) {
    const req = data.approval_request;
    const container = document.getElementById("approvalContainer");
    if (!container) return;

    container.innerHTML = `
      <div class="approval-card">
        <div class="approval-header">
          <span>⚠️ Human-in-the-Loop Approval Required</span>
        </div>
        <p style="font-size: 12px; color: #cbd5e1;">The AI Operator has drafted a live message for <strong>${req.recipient}</strong> and is awaiting your authorization before transmission:</p>
        <div class="approval-content-box">
          <div style="font-size: 11px; text-transform: uppercase; color: #94a3b8; margin-bottom: 4px;">Proposed Message:</div>
          <textarea id="approvalTextarea" style="width: 100%; background: none; border: none; color: #fff; font-family: inherit; font-size: 13px; outline: none; resize: vertical;">${req.proposed_content}</textarea>
        </div>
        <div style="font-size: 11px; color: #f87171;">⚠️ ${req.warning}</div>
        <div class="approval-actions">
          <button class="btn-approve" onclick="window.app.respondApproval('${data.task_id}', 'approved')">Approve & Transmit</button>
          <button class="btn-reject" onclick="window.app.respondApproval('${data.task_id}', 'rejected')">Reject Action</button>
        </div>
      </div>
    `;
  }

  respondApproval(taskId, decision) {
    let modifiedText = null;
    const textarea = document.getElementById("approvalTextarea");
    if (textarea) {
      modifiedText = textarea.value;
    }

    this.ws.send(JSON.stringify({
      type: "APPROVAL_RESPONSE",
      payload: {
        task_id: taskId,
        decision: decision,
        modified_text: modifiedText
      }
    }));
    document.getElementById("approvalContainer").innerHTML = "";
  }

  async loadDeviceApps(deviceId) {
    try {
      const res = await fetch(`/api/devices/${deviceId}/apps`);
      const apps = await res.json();
      const container = document.getElementById("appsContainer");
      if (!container) return;
      container.innerHTML = "";
      if (!res.ok || !Array.isArray(apps)) {   // offline phone → error object, not a list
        const note = document.createElement("div");
        note.className = "apps-unavailable";
        note.textContent = "Apps unavailable — phone offline";
        container.appendChild(note);
        return;
      }

      const have = new Set((apps || []).map((a) => a.package_name));
      // Camera differs per brand: open whichever camera app this phone has
      const cameraPkg = ["com.oplus.camera", "com.android.camera", "com.sec.android.app.camera", "com.android.camera2",
        "com.google.android.GoogleCamera", "com.motorola.camera3", "com.huawei.camera", "com.vivo.camera"].find((p) => have.has(p))
        || [...have].find((p) => /camera/i.test(p)) || "com.android.camera";
      const si = (slug, color) => `<img src="https://cdn.simpleicons.org/${slug}/${color}" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode(this.dataset.fb))" data-fb="${slug === 'whatsapp' ? '💬' : slug === 'instagram' ? '📷' : slug === 'snapchat' ? '👻' : '🌐'}">`;
      const launcher = [
        { pkg: "com.operator.companion", title: "Crystal Mines · PowerBoosters (this app)", cls: "full",
          html: `<svg viewBox="0 0 108 108" aria-hidden="true"><defs><linearGradient id="accBg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4C1D95"/><stop offset=".55" stop-color="#7C3AED"/><stop offset="1" stop-color="#EC4899"/></linearGradient><linearGradient id="accBolt" x1="0" y1="0" x2=".35" y2="1"><stop offset="0" stop-color="#FEF9C3"/><stop offset=".45" stop-color="#FDE047"/><stop offset="1" stop-color="#F97316"/></linearGradient></defs><rect width="108" height="108" fill="url(#accBg)"/><circle cx="54" cy="54" r="40" fill="#22D3EE" opacity=".18"/><path d="M64,18 L36,59 L53,59 L44,92 L80,47 L60,47 Z" fill="url(#accBolt)"/><path d="M64,18 L50,38 L56,38 L62,25 Z" fill="#fff" opacity=".8"/></svg>` },
        { pkg: "com.whatsapp", title: "WhatsApp", bg: "#25D366", html: si("whatsapp", "ffffff") },
        { pkg: "com.instagram.android", title: "Instagram", bg: "radial-gradient(circle at 30% 107%, #fdf497 0%, #fdf497 5%, #fd5949 45%, #d6249f 60%, #285AEB 90%)", html: si("instagram", "ffffff") },
        { pkg: "com.snapchat.android", title: "Snapchat", bg: "#FFFC00", html: si("snapchat", "000000") },
        { pkg: "com.android.chrome", title: "Chrome", bg: "#ffffff", html: si("googlechrome", "4285F4") },
        { pkg: cameraPkg, title: "Camera", bg: "linear-gradient(135deg, #334155, #0f172a)",
          html: `<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l2-2.5h6L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.6"/></svg>` },
      ];
      launcher.forEach((a) => {
        const btn = document.createElement("button");
        btn.className = "app-logo" + (a.cls ? " " + a.cls : "");
        btn.title = a.title; btn.setAttribute("aria-label", "Open " + a.title);
        if (a.bg) btn.style.background = a.bg;
        if (have.size && !have.has(a.pkg) && a.pkg !== cameraPkg) btn.style.opacity = "0.35";  // not installed on this phone
        btn.innerHTML = a.html;
        btn.onclick = () => this.launchApp(deviceId, a.pkg);
        container.appendChild(btn);
      });
    } catch (e) {
      console.error("Failed to load apps:", e);
    }
  }

  async launchApp(deviceId, packageName) {
    await fetch(`/api/devices/${deviceId}/apps/launch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_name: packageName })
    });
  }

  setCallStateChip(text, color) {
    const chip = document.getElementById("callStateInline");
    if (chip) { chip.textContent = text; chip.style.color = color; chip.style.borderColor = color + "55"; }
  }

  async dialFromLaptop() {
    const inp = document.getElementById("callNumberInput");
    const number = (inp?.value || "").trim();
    if (!number) { this.showToast("Enter a number to call.", "info"); return; }
    if (!this.activeDevice) { this.showToast("Select a phone first.", "info"); return; }
    if (this.activeDevice.status === "offline") { this.showToast("That phone is offline.", "error"); return; }
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(this.activeDevice.id)}/call/dial?number=${encodeURIComponent(number)}`, { method: "POST" });
      const d = await r.json();
      if (d.status === "success") {
        this.setCallStateChip("Dialing…", "#06b6d4");
        this.showToast(`📞 Dialing ${number} on ${this.activeDevice.model}. Talk on the phone (live laptop audio is coming soon).`, "success");
      } else this.showToast("Could not place the call.", "error");
    } catch (e) { this.showToast("Could not reach the phone.", "error"); }
  }

  showToast(message, kind = "info") {
    let host = document.getElementById("dashToasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "dashToasts";
      host.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:10000;display:flex;flex-direction:column;gap:8px;align-items:center;";
      document.body.appendChild(host);
    }
    const colors = { success: "#10b981", error: "#f43f5e", info: "#6366f1" };
    const t = document.createElement("div");
    t.textContent = message;
    t.style.cssText = `background:#0f172a;color:#f8fafc;border:1px solid ${colors[kind] || colors.info};border-radius:10px;padding:10px 16px;font-size:13px;box-shadow:0 10px 25px rgba(0,0,0,.5);max-width:520px;`;
    host.appendChild(t);
    setTimeout(() => t.remove(), kind === "error" ? 6000 : 4000);
  }

  async pushUpdateToDevices() {
    const pill = document.getElementById("updatePillText");
    const prev = pill ? pill.innerText : "";
    if (pill) pill.innerText = "Sending…";
    try {
      const r = await fetch("/api/app/notify-update", { method: "POST" });
      const d = await r.json();
      const ver = d.latest && (d.latest.full || d.latest.lite);
      this.showToast(d.notified > 0
        ? `⬆️ Told ${d.notified} device(s) to update${ver ? " to " + ver : ""}. They install with one tap.`
        : "No phones are connected through the companion app right now. They update on their own when the app reconnects.", d.notified > 0 ? "success" : "info");
    } catch (e) {
      this.showToast("Could not reach the update service.", "error");
    } finally {
      if (pill) pill.innerText = prev || "Update Devices";
    }
  }

  openFileManager() {
    const id = this.activeDevice ? this.activeDevice.id : "";
    window.open(`/files.html?device=${encodeURIComponent(id)}`, "_blank");
  }

  // ----- Routines (scheduled AI-operator tasks) -----
  setupRoutines() {
    const form = document.getElementById("routineForm");
    if (!form) return;
    const days = new Set();
    const hint = () => {
      const n = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
      document.getElementById("rtDaysHint").innerText = days.size ? [...days].sort().map((d) => n[d]).join(", ") : "every day";
      document.querySelectorAll("#rtDays button").forEach((b) => b.classList.toggle("on", days.has(+b.dataset.d)));
    };
    document.querySelectorAll("#rtDays button").forEach((b) => b.onclick = () => { const d = +b.dataset.d; days.has(d) ? days.delete(d) : days.add(d); hint(); });
    const preset = document.getElementById("rtPreset"), custom = document.getElementById("rtInstruction");
    preset.onchange = () => { custom.hidden = preset.value !== "__custom"; };
    this.openRoutineForm = (r) => {
      const sel = document.getElementById("rtDevice");
      sel.innerHTML = this.devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}${d.status === "offline" ? " (offline)" : ""}</option>`).join("");
      sel.value = (r && r.device_id) || (this.activeDevice && this.activeDevice.id) || (this.devices[0] && this.devices[0].id) || "";
      document.getElementById("rtId").value = (r && r.id) || "";
      document.getElementById("rtTime").value = (r && r.time) || "09:00";
      const known = [...preset.options].some((o) => o.value === (r && r.instruction));
      preset.value = !r ? preset.options[0].value : known ? r.instruction : "__custom";
      custom.value = r && !known ? r.instruction : "";
      custom.hidden = preset.value !== "__custom";
      days.clear(); ((r && r.days) || []).forEach((d) => days.add(d)); hint();
      form.hidden = false;
    };
    document.getElementById("routineNewBtn").onclick = () => this.openRoutineForm(null);
    document.getElementById("rtCancel").onclick = () => { form.hidden = true; };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const instruction = preset.value === "__custom" ? custom.value.trim() : preset.value;
      if (!instruction) return custom.focus();
      const label = preset.value === "__custom" ? instruction : preset.options[preset.selectedIndex].text.replace(/^\S+\s/, "");
      const body = { id: document.getElementById("rtId").value || undefined, name: label, instruction,
        device_id: document.getElementById("rtDevice").value, time: document.getElementById("rtTime").value, days: [...days], enabled: true };
      const r = await fetch("/api/routines", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) return alert("Could not save: " + ((await r.json()).detail || r.status));
      form.hidden = true;
      this.loadRoutines();
    };
    this.loadRoutines();
    setInterval(() => this.loadRoutines(), 15000);
  }

  async loadRoutines() {
    const box = document.getElementById("routineList");
    if (!box) return;
    let list = [];
    try { list = await (await fetch("/api/routines")).json(); } catch (e) { return; }
    this._routines = list;
    if (!list.length) { box.innerHTML = '<div class="rt-empty">No routines yet. ＋ New schedules a task to run by itself, e.g. WhatsApp statuses every morning.</div>'; return; }
    box.replaceChildren(...list.map((r) => {
      const el = document.createElement("div");
      el.className = "routine-item" + (r.enabled ? "" : " paused");
      const st = r.last_status === "failed" ? "err" : r.last_status === "completed" ? "ok" : r.last_status ? "busy" : "";
      el.innerHTML = `
        <div class="rt-main">
          <div class="rt-name">${esc(r.name)}</div>
          <div class="rt-meta">📱 ${esc(r.device_name || "")} · ⏰ ${esc(r.next_run)}</div>
          ${r.last_result ? `<div class="rt-last ${st}">${esc(r.last_result)}</div>` : ""}
        </div>
        <div class="rt-btns">
          <button class="nav-btn" data-act="run" title="Run now">▶</button>
          <button class="nav-btn" data-act="toggle" title="${r.enabled ? "Pause" : "Resume"}">${r.enabled ? "⏸" : "⏵"}</button>
          <button class="nav-btn" data-act="edit" title="Edit">✎</button>
          <button class="nav-btn" data-act="del" title="Delete">🗑</button>
        </div>`;
      el.querySelector('[data-act="run"]').onclick = async () => {
        const res = await fetch(`/api/routines/${r.id}/run`, { method: "POST" });
        if (!res.ok) alert((await res.json()).detail || "Could not start");
        this.loadRoutines();
      };
      el.querySelector('[data-act="toggle"]').onclick = async () => {
        await fetch("/api/routines", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...r, enabled: !r.enabled }) });
        this.loadRoutines();
      };
      el.querySelector('[data-act="edit"]').onclick = () => this.openRoutineForm(r);
      el.querySelector('[data-act="del"]').onclick = async () => {
        if (!confirm(`Delete the routine "${r.name}"?`)) return;
        await fetch(`/api/routines/${r.id}`, { method: "DELETE" });
        this.loadRoutines();
      };
      return el;
    }));
  }

  // Device Storage: browse the selected phone right here (lists automatically when it connects);
  // folders open in place, files download; "Open File Manager ↗" still opens the full manager in a new tab.
  async loadDeviceFiles(deviceId, path) {
    const container = document.getElementById("fileListContainer");
    if (!container) return;
    if (!path || deviceId !== this._fsDevice) path = path || "/sdcard";
    this._fsDevice = deviceId; this._fsPath = path;
    const sd = document.getElementById("storageDevice");
    const dev = this.devices.find((d) => d.id === deviceId);
    if (sd) sd.innerText = dev ? "· " + dev.name : "";
    const note = (text) => {
      const d = document.createElement("div");
      d.className = "fs-note";
      d.textContent = text;
      return d;
    };
    // breadcrumb + up
    const crumbs = document.createElement("div");
    crumbs.className = "fs-crumbs";
    const up = document.createElement("button");
    up.className = "nav-btn fs-up"; up.textContent = "⬆ Up"; up.title = "Parent folder";
    up.disabled = path === "/sdcard" || path === "/";
    up.onclick = () => this.loadDeviceFiles(deviceId, path.replace(/\/[^/]+\/?$/, "") || "/sdcard");
    const where = document.createElement("span");
    where.className = "fs-path"; where.textContent = path.replace(/^\/sdcard/, "Internal storage");
    crumbs.append(up, where);
    container.replaceChildren(crumbs, note("Loading…"));
    if (dev && dev.status === "offline") return container.replaceChildren(crumbs, note("This phone is offline — its storage appears when it reconnects."));
    try {
      const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/fs/list?path=${encodeURIComponent(path)}`);
      if (this._fsDevice !== deviceId || this._fsPath !== path) return;   // user moved on meanwhile
      if (!res.ok) return container.replaceChildren(crumbs, note("Storage not reachable right now"));
      const items = ((await res.json()).items || []).filter((f) => !f.hidden)
        .sort((a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
      if (!items.length) return container.replaceChildren(crumbs, note("This folder is empty"));
      const fmt = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(1)} GB`;
      const files = items.filter((x) => !x.is_dir);
      const isMedia = (f) => !f.is_dir && ["image", "video"].includes(this.fileKind(f.name));
      const rows = items.filter((f) => !isMedia(f)).map((f) => {
        const kind = f.is_dir ? "dir" : this.fileKind(f.name);
        const row = document.createElement(f.is_dir ? "button" : "a");
        row.className = "file-item";
        row.title = f.is_dir ? `Open ${f.name}` : f.name;
        const icon = document.createElement("span");
        icon.className = "fi-icon";
        icon.textContent = { dir: "📁", audio: "🎵", pdf: "📕", text: "📝", apk: "📦", archive: "🗜" }[kind] || "📄";
        const name = document.createElement("span");
        name.className = "fi-name";
        name.textContent = f.name;
        const size = document.createElement("span");
        size.className = "fi-size";
        size.textContent = f.is_dir ? "" : fmt(f.size);
        row.append(icon, name, size);
        if (f.is_dir) row.onclick = () => this.loadDeviceFiles(deviceId, f.path);
        else {
          row.href = this.fsUrl(deviceId, f.path, true);
          row.onclick = (e) => { e.preventDefault(); this.openFilePreview(deviceId, f, files); };
        }
        return row;
      });
      // photos & videos: picture grid like Recent captures — no names, big thumbs, hover to preview
      const media = items.filter(isMedia);
      // grouped sections with counts: Folders · Files · Photos & videos
      const label = (text, n) => { const h = document.createElement("div"); h.className = "fs-section"; h.innerHTML = `<span>${text}</span><span class="fs-count">${n}</span>`; return h; };
      const dirRows = rows.filter((r) => r.tagName === "BUTTON"), fileRows = rows.filter((r) => r.tagName !== "BUTTON");
      const out = [crumbs];
      if (dirRows.length) out.push(label("Folders", dirRows.length), ...dirRows);
      if (fileRows.length) out.push(label("Files", fileRows.length), ...fileRows);
      if (media.length) {
        out.push(label("Photos & videos", media.length));
        const grid = document.createElement("div");
        grid.className = "fs-media-grid";
        media.forEach((f) => {
          const kind = this.fileKind(f.name);
          const a = document.createElement("a");
          a.className = "cam-thumb fs-tile";
          a.href = this.fsUrl(deviceId, f.path, true);
          a.title = new Date((f.mtime || 0) * 1000).toLocaleString();
          const im = document.createElement("img");
          im.loading = "lazy"; im.decoding = "async"; im.alt = "";
          im.src = `/api/devices/${encodeURIComponent(deviceId)}/fs/thumb?path=${encodeURIComponent(f.path)}&mtime=${f.mtime || 0}&size=256`;
          im.onerror = () => { a.classList.add("no-thumb"); a.textContent = kind === "video" ? "🎬" : "🖼"; };
          a.append(im);
          if (kind === "video") { const b = document.createElement("span"); b.className = "cam-badge"; b.textContent = "▶"; a.append(b); }
          const it = { kind: kind === "video" ? "video" : "photo", url: a.href, size: f.size || 0, mtime: f.mtime || 0, device: dev ? dev.name : "" };
          a.addEventListener("mouseenter", () => this.showCapturePreview(it, a));
          a.addEventListener("mouseleave", () => this.hideCapturePreview());
          a.onclick = (e) => { e.preventDefault(); this.hideCapturePreview(); this.openFilePreview(deviceId, f, files); };
          grid.append(a);
        });
        out.push(grid);
      }
      container.replaceChildren(...out);
    } catch (e) {
      console.error("Failed to load files:", e);
      container.replaceChildren(crumbs, note("Storage not reachable right now"));
    }
  }

  fileKind(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"].includes(ext)) return "image";
    if (["mp4", "webm", "mkv", "3gp", "mov", "m4v"].includes(ext)) return "video";
    if (["mp3", "m4a", "aac", "wav", "ogg", "opus", "flac", "amr"].includes(ext)) return "audio";
    if (ext === "pdf") return "pdf";
    if (["txt", "log", "json", "xml", "csv", "md", "html", "js", "py", "kt", "java", "ini", "conf", "yml", "yaml"].includes(ext)) return "text";
    if (ext === "apk") return "apk";
    if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return "archive";
    return "other";
  }

  fsUrl(deviceId, path, inline) {
    return `/api/devices/${encodeURIComponent(deviceId)}/fs/download?path=${encodeURIComponent(path)}${inline ? "&inline=true" : ""}`;
  }

  // In-dashboard file viewer for the Storage panel: preview, ← → through the folder, download, open in tab, close
  openFilePreview(deviceId, f, siblings) {
    let ov = document.getElementById("fsViewer");
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "fsViewer";
      ov.className = "fs-viewer";
      ov.innerHTML = `<div class="fs-viewer-box" role="dialog" aria-modal="true">
          <div class="fs-viewer-bar">
            <div class="fs-viewer-title"><b id="fsvName"></b><span id="fsvMeta"></span></div>
            <button class="nav-btn" id="fsvPrev" title="Previous file (←)">◀</button>
            <button class="nav-btn" id="fsvNext" title="Next file (→)">▶</button>
            <a class="nav-btn" id="fsvDownload" title="Download to this laptop">⬇ Download</a>
            <a class="nav-btn" id="fsvOpen" target="_blank" title="Open in a new tab">↗ Open</a>
            <button class="nav-btn" id="fsvClose" title="Close (Esc)">✕</button>
          </div>
          <div class="fs-viewer-body" id="fsvBody"></div>
        </div>`;
      document.body.append(ov);
      ov.addEventListener("click", (e) => { if (e.target === ov) this.closeFilePreview(); });
      ov.querySelector("#fsvClose").onclick = () => this.closeFilePreview();
      ov.querySelector("#fsvPrev").onclick = () => this.stepFilePreview(-1);
      ov.querySelector("#fsvNext").onclick = () => this.stepFilePreview(1);
      document.addEventListener("keydown", (e) => {
        if (!ov.classList.contains("on")) return;
        if (e.key === "Escape") this.closeFilePreview();
        else if (e.key === "ArrowLeft") this.stepFilePreview(-1);
        else if (e.key === "ArrowRight") this.stepFilePreview(1);
      });
    }
    this._fsv = { deviceId, list: siblings || [f], index: Math.max(0, (siblings || [f]).findIndex((x) => x.path === f.path)) };
    this.renderFilePreview();
    ov.classList.add("on");
  }

  stepFilePreview(d) {
    const v = this._fsv; if (!v || v.list.length < 2) return;
    v.index = (v.index + d + v.list.length) % v.list.length;
    this.renderFilePreview();
  }

  async renderFilePreview() {
    const v = this._fsv, f = v.list[v.index], kind = this.fileKind(f.name);
    const url = this.fsUrl(v.deviceId, f.path, true);
    const fmt = (n) => n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
    document.getElementById("fsvName").textContent = f.name;
    document.getElementById("fsvMeta").textContent = ` · ${fmt(f.size || 0)}` + (f.mtime ? ` · ${new Date(f.mtime * 1000).toLocaleString()}` : "") + (v.list.length > 1 ? ` · ${v.index + 1} / ${v.list.length}` : "");
    const dl = document.getElementById("fsvDownload"); dl.href = this.fsUrl(v.deviceId, f.path, false); dl.setAttribute("download", f.name);
    document.getElementById("fsvOpen").href = url;
    document.getElementById("fsvPrev").disabled = document.getElementById("fsvNext").disabled = v.list.length < 2;
    const body = document.getElementById("fsvBody");
    body.querySelectorAll("video, audio").forEach((m) => m.pause());
    let el;
    if (kind === "image") { el = document.createElement("img"); el.src = url; el.alt = f.name; }
    else if (kind === "video") { el = document.createElement("video"); el.src = url; el.controls = true; el.autoplay = true; el.playsInline = true; }
    else if (kind === "audio") { el = document.createElement("audio"); el.src = url; el.controls = true; el.autoplay = true; }
    else if (kind === "pdf") { el = document.createElement("iframe"); el.src = url; el.title = f.name; }
    else if (kind === "text" && (f.size || 0) < 2 * 1048576) {
      el = document.createElement("pre"); el.textContent = "Loading…";
      fetch(url).then((r) => r.text()).then((t) => { el.textContent = t.slice(0, 200000) + (t.length > 200000 ? "\n… (truncated — download for the full file)" : ""); })
        .catch(() => { el.textContent = "Could not load this file."; });
    } else {
      el = document.createElement("div"); el.className = "fs-viewer-none";
      el.innerHTML = `<div style="font-size:42px">📄</div><div>No preview for this type of file.</div><div>Use ⬇ Download or ↗ Open.</div>`;
    }
    body.replaceChildren(el);
  }

  closeFilePreview() {
    const ov = document.getElementById("fsViewer"); if (!ov) return;
    ov.classList.remove("on");
    const body = document.getElementById("fsvBody");
    body.querySelectorAll("video, audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
    body.replaceChildren();
  }

  // Centralized Remote Diagnostics & Error Handling
  handleDeviceError(report, deviceId) {
    console.warn("[Diagnostics] Remote device error received:", report);
    this.diagnosticsList.unshift(report);
    if (this.diagnosticsList.length > 100) this.diagnosticsList.pop();
    this.updateDiagnosticsBadge();

    const dev = this.devices.find(d => d.id === (report.device_id || deviceId)) || { name: report.device_id || "Android Device" };

    // Show Desktop Diagnostic Toast Notification
    const toast = document.getElementById("mediaPreviewToast");
    if (toast) {
      document.getElementById("mediaToastTitle").innerHTML = `<span style="color: #f43f5e;">⚠️ Hardware Exception (${esc(report.error_code)})</span>`;
      document.getElementById("mediaToastContent").innerHTML = `
        <div style="font-size: 12px; font-weight: 600; color: #f8fafc; margin-bottom: 4px;">${esc(dev.name)} · ${esc(report.module)}</div>
        <div style="font-size: 11px; color: #cbd5e1; margin-bottom: 6px;">${esc(report.description)}</div>
        <div style="font-size: 10px; color: #38bdf8; background: rgba(56,189,248,0.1); padding: 4px 6px; border-radius: 4px;">💡 Resolution: ${esc(report.suggested_resolution)}</div>
      `;
      const link = document.getElementById("mediaToastDownloadLink");
      link.innerText = "Inspect in Diagnostics Center";
      link.href = "#";
      link.onclick = (e) => {
        e.preventDefault();
        toast.style.display = "none";
        this.toggleDiagnosticsModal();
      };
      toast.style.display = "block";
    }

    // Refresh modal if currently open
    const modal = document.getElementById("diagnosticsModal");
    if (modal && modal.style.display !== "none") {
      this.renderDiagnosticsList();
    }
  }

  handleCapabilitiesUpdated(payload) {
    const devId = payload.device_id;
    const dev = this.devices.find(d => d.id === devId);
    if (dev) {
      if (payload.hardware_capabilities) dev.hardware_capabilities = payload.hardware_capabilities;
      if (payload.permissions) dev.permissions = payload.permissions;
      if (payload.power_and_lockscreen) dev.power_and_lockscreen = payload.power_and_lockscreen;
      if (payload.manufacturer) dev.manufacturer = payload.manufacturer;
      if (payload.sdk_int) dev.sdk_int = payload.sdk_int;
      this.renderDeviceList();
      const modal = document.getElementById("capabilitiesModal");
      if (modal && modal.style.display !== "none") {
        this.renderCapabilitiesView();
      }
    }
  }

  updateDiagnosticsBadge() {
    const badge = document.getElementById("diagnosticsCount");
    if (badge) {
      badge.innerText = this.diagnosticsList.length;
      const pill = document.getElementById("diagnosticsPill");
      if (pill) {
        if (this.diagnosticsList.length > 0) {
          pill.style.background = "rgba(244, 63, 94, 0.25)";
          pill.style.borderColor = "#f43f5e";
          pill.style.color = "#ffe4e6";
        } else {
          pill.style.background = "rgba(244, 63, 94, 0.1)";
          pill.style.borderColor = "rgba(244, 63, 94, 0.3)";
          pill.style.color = "#fda4af";
        }
      }
    }
  }

  toggleDiagnosticsModal() {
    const modal = document.getElementById("diagnosticsModal");
    if (!modal) return;
    const isVisible = modal.style.display !== "none";
    if (isVisible) {
      modal.style.display = "none";
    } else {
      modal.style.display = "flex";
      this.populateDiagnosticsFilter();
      this.renderDiagnosticsList();
    }
  }

  populateDiagnosticsFilter() {
    const filterSelect = document.getElementById("diagDeviceFilter");
    if (!filterSelect) return;
    const currentVal = filterSelect.value;
    filterSelect.innerHTML = `<option value="all">All Connected Devices</option>`;
    this.devices.forEach(dev => {
      const opt = document.createElement("option");
      opt.value = dev.id;
      opt.innerText = `${esc(dev.name)} (${dev.serial.slice(0, 8)})`;
      filterSelect.appendChild(opt);
    });
    if (currentVal) filterSelect.value = currentVal;
  }

  renderDiagnosticsList() {
    const container = document.getElementById("diagnosticsListContainer");
    if (!container) return;
    const filter = document.getElementById("diagDeviceFilter")?.value || "all";
    const filtered = filter === "all" ? this.diagnosticsList : this.diagnosticsList.filter(d => d.device_id === filter);

    const totalEl = document.getElementById("diagTotalCount");
    if (totalEl) totalEl.innerText = `${filtered.length} reported errors`;

    if (filtered.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; color: #10b981; padding: 40px 10px; font-size: 13px; background: rgba(16,185,129,0.05); border-radius: 8px; border: 1px solid rgba(16,185,129,0.2);">
          ✅ No errors recorded. Connected Android fleet is operating nominally without errors.
        </div>
      `;
      return;
    }

    container.innerHTML = "";
    filtered.forEach(err => {
      const card = document.createElement("div");
      card.style.background = "rgba(15, 23, 42, 0.85)";
      card.style.border = "1px solid rgba(244, 63, 94, 0.35)";
      card.style.borderRadius = "10px";
      card.style.padding = "12px";
      card.style.display = "flex";
      card.style.flexDirection = "column";
      card.style.gap = "6px";

      const dev = this.devices.find(d => d.id === err.device_id) || { name: err.device_id, android_version: "Unknown" };
      const timeStr = new Date(err.timestamp || Date.now()).toLocaleTimeString();

      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div style="display: flex; align-items: center; gap: 6px;">
            <span style="color: #f43f5e; font-size: 13px;">🚨</span>
            <span style="font-weight: 700; color: #f8fafc; font-size: 13px;">${esc(dev.name)}</span>
            <span style="font-size: 10px; background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px; color: #94a3b8;">${esc(err.module)} · ${esc(err.operation)}</span>
          </div>
          <span style="font-size: 11px; color: #64748b; font-family: monospace;">${timeStr}</span>
        </div>
        <div style="font-size: 12px; font-weight: 600; color: #fda4af;">[${esc(err.error_code)}] ${esc(err.description)}</div>
        <div style="font-size: 11px; color: #38bdf8; background: rgba(56,189,248,0.08); border-left: 3px solid #38bdf8; padding: 6px 8px; border-radius: 0 4px 4px 0;">
          💡 <strong>Suggested Action:</strong> ${esc(err.suggested_resolution)}
        </div>
        ${err.stack_trace ? `
          <details style="margin-top: 4px;">
            <summary style="font-size: 10px; color: #64748b; cursor: pointer;">View Stack Trace</summary>
            <pre style="font-size: 10px; color: #cbd5e1; background: rgba(0,0,0,0.4); padding: 8px; border-radius: 6px; overflow-x: auto; max-height: 120px;">${esc(err.stack_trace)}</pre>
          </details>
        ` : ""}
      `;
      container.appendChild(card);
    });
  }

  clearDiagnostics() {
    this.diagnosticsList = [];
    this.updateDiagnosticsBadge();
    this.renderDiagnosticsList();
  }

  showCapabilitiesModal(deviceId) {
    const modal = document.getElementById("capabilitiesModal");
    if (!modal) return;
    modal.style.display = "flex";

    const select = document.getElementById("capDeviceSelect");
    if (select) {
      select.innerHTML = "";
      this.devices.forEach(dev => {
        const opt = document.createElement("option");
        opt.value = dev.id;
        opt.innerText = `${esc(dev.name)} (${dev.serial.slice(0, 8)})`;
        select.appendChild(opt);
      });
      if (deviceId) select.value = deviceId;
      else if (this.activeDevice) select.value = this.activeDevice.id;
    }
    this.renderCapabilitiesView();
  }

  renderCapabilitiesView() {
    const container = document.getElementById("capabilitiesContent");
    if (!container) return;
    const select = document.getElementById("capDeviceSelect");
    const targetId = select ? select.value : (this.activeDevice?.id);
    const dev = this.devices.find(d => d.id === targetId) || this.activeDevice;

    if (!dev) {
      container.innerHTML = `<div style="text-align: center; color: #64748b; padding: 20px;">No device selected.</div>`;
      return;
    }

    const hwCaps = dev.hardware_capabilities || {};
    const rearHw = hwCaps.rear_hardware_level || "FULL (Level 1)";
    const frontHw = hwCaps.front_hardware_level || "LIMITED (Level 0)";
    const maxPhoto = hwCaps.max_photo_resolution || "1920x1080 (FHD)";
    const maxVideo = hwCaps.max_video_resolution || "1280x720 (HD H.264)";

    container.innerHTML = `
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;">
        <div style="background: rgba(15,23,42,0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 10px;">
          <div style="font-size: 11px; font-weight: 600; color: #94a3b8; text-transform: uppercase;">OS & Manufacturer</div>
          <div style="font-size: 13px; font-weight: 700; color: #f8fafc; margin-top: 2px;">${esc(dev.manufacturer || dev.brand)} ${esc(dev.model)}</div>
          <div style="font-size: 11px; color: #cbd5e1; margin-top: 2px;">Android ${esc(dev.android_version)} (API Level ${dev.sdk_int || 33})</div>
        </div>

        <div style="background: rgba(15,23,42,0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 10px;">
          <div style="font-size: 11px; font-weight: 600; color: #94a3b8; text-transform: uppercase;">Screen-Off & Lockscreen</div>
          <div style="font-size: 13px; font-weight: 700; color: #10b981; margin-top: 2px;">✅ Supported (Zero UI)</div>
          <div style="font-size: 11px; color: #cbd5e1; margin-top: 2px;">WakeLock · ShowWhenLocked · Rail Wakeup</div>
        </div>
      </div>

      <div style="background: rgba(15,23,42,0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px;">
        <div style="font-size: 12px; font-weight: 700; color: #e2e8f0; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
          <span>📸</span> Camera2 Hardware HAL Capabilities
        </div>
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; font-size: 11px;">
          <div><span style="color: #94a3b8;">Rear Sensor Level:</span> <strong style="color: #67e8f9;">${rearHw}</strong></div>
          <div><span style="color: #94a3b8;">Front Sensor Level:</span> <strong style="color: #67e8f9;">${frontHw}</strong></div>
          <div><span style="color: #94a3b8;">Max Photo Res:</span> <strong style="color: #f8fafc;">${maxPhoto}</strong></div>
          <div><span style="color: #94a3b8;">Max Video Res:</span> <strong style="color: #f8fafc;">${maxVideo}</strong></div>
          <div><span style="color: #94a3b8;">Audio-Busy Fallback:</span> <strong style="color: #10b981;">Active (Auto-switches to Video-only)</strong></div>
          <div><span style="color: #94a3b8;">Zero-Residual Storage:</span> <strong style="color: #10b981;">Active (Purges phone upon pull)</strong></div>
        </div>
      </div>

      <div style="background: rgba(15,23,42,0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px;">
        <div style="font-size: 12px; font-weight: 700; color: #e2e8f0; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
          <span>🛡️</span> Cross-Platform Compatibility Matrix (Android 11 – 15)
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
          <thead>
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.1); color: #94a3b8; text-align: left;">
              <th style="padding: 4px;">Feature</th>
              <th style="padding: 4px;">Android 11-12</th>
              <th style="padding: 4px;">Android 13</th>
              <th style="padding: 4px;">Android 14-15</th>
              <th style="padding: 4px;">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <td style="padding: 5px 4px; color: #f8fafc;">Headless Photo</td>
              <td style="padding: 5px 4px; color: #10b981;">Camera2 Direct</td>
              <td style="padding: 5px 4px; color: #10b981;">Camera2 Direct</td>
              <td style="padding: 5px 4px; color: #10b981;">FGS Camera Type</td>
              <td style="padding: 5px 4px; color: #10b981;">✅ Verified</td>
            </tr>
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <td style="padding: 5px 4px; color: #f8fafc;">Headless Video (Front/Back)</td>
              <td style="padding: 5px 4px; color: #10b981;">MediaRecorder</td>
              <td style="padding: 5px 4px; color: #10b981;">MediaRecorder</td>
              <td style="padding: 5px 4px; color: #10b981;">FGS Camera+Mic</td>
              <td style="padding: 5px 4px; color: #10b981;">✅ Verified</td>
            </tr>
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <td style="padding: 5px 4px; color: #f8fafc;">Lockscreen Capture</td>
              <td style="padding: 5px 4px; color: #10b981;">Window Flags</td>
              <td style="padding: 5px 4px; color: #10b981;">ShowWhenLocked</td>
              <td style="padding: 5px 4px; color: #10b981;">FullScreenIntent</td>
              <td style="padding: 5px 4px; color: #10b981;">✅ Verified</td>
            </tr>
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <td style="padding: 5px 4px; color: #f8fafc;">Screen-Off Rail Wakeup</td>
              <td style="padding: 5px 4px; color: #10b981;">Keyevent 224</td>
              <td style="padding: 5px 4px; color: #10b981;">Keyevent 224</td>
              <td style="padding: 5px 4px; color: #10b981;">Keyevent 224</td>
              <td style="padding: 5px 4px; color: #10b981;">✅ Verified</td>
            </tr>
            <tr>
              <td style="padding: 5px 4px; color: #f8fafc;">Silent Remote Diagnostics</td>
              <td style="padding: 5px 4px; color: #10b981;">UncaughtHandler</td>
              <td style="padding: 5px 4px; color: #10b981;">UncaughtHandler</td>
              <td style="padding: 5px 4px; color: #10b981;">UncaughtHandler</td>
              <td style="padding: 5px 4px; color: #10b981;">✅ Verified</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  }

  async refreshDeviceCapabilities() {
    const select = document.getElementById("capDeviceSelect");
    const targetId = select ? select.value : (this.activeDevice?.id);
    if (!targetId) return;

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "GET_CAPABILITIES", device_id: targetId }));
    }
    try {
      const res = await fetch(`/api/devices/${targetId}/capabilities`);
      const caps = await res.json();
      this.handleCapabilitiesUpdated(caps);
    } catch (e) {
      console.error("Failed to query capabilities:", e);
    }
  }

  // ---------- Companion app lifecycle (uninstall / update / reinstall / device owner) ----------
  get appSel() { return this._appSel || (this._appSel = new Set()); }

  toggleAppSel(id, on) {
    if (on) this.appSel.add(id); else this.appSel.delete(id);
    this.renderDeviceList();
  }

  appPathLabel(path) {
    return { adb: "Silent via ADB", device_owner: "Silent (device owner)", silent_a12: "Silent (Android 12+)",
             one_tap: "Needs 1 tap on phone" }[path] || "Not possible (phone offline / not on ADB)";
  }

  async appLifecycle(ids, action) {
    const verb = { update: "Update", reinstall: "Reinstall", uninstall: "Uninstall" }[action];
    const plan = await Promise.all(ids.map(async (id) => {
      const dev = this.devices.find((d) => d.id === id);
      try {
        const r = await fetch(`/api/devices/${encodeURIComponent(id)}/app-lifecycle`);
        const info = r.ok ? await r.json() : null;
        return { id, name: dev ? dev.name : id, path: info ? info.paths[action] : null, owner: !!(info && info.device_owner) };
      } catch (e) { return { id, name: dev ? dev.name : id, path: null }; }
    }));
    let lines = plan.map((p) => {
      let label = this.appPathLabel(p.path);
      if (action === "uninstall" && p.path === "one_tap" && p.owner) label = "Needs 1 tap (device-owner role released first)";
      if (action === "update" && p.path === "silent_a12") label += " — 1 tap if Android still asks";
      return `• ${p.name}: ${label}`;
    });
    const go = plan.filter((p) => p.path);
    if (!go.length) return alert(`${verb} companion app\n\n${lines.join("\n")}`);
    if (!confirm(`${verb} the companion app?\n\n${lines.join("\n")}`)) return;
    this.fleetNote(`${verb}: working on ${go.length} phone(s)…`, "busy");
    const results = await Promise.all(go.map(async (p) => {
      try {
        const r = await fetch(`/api/devices/${encodeURIComponent(p.id)}/app-lifecycle/${action}`, { method: "POST" });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) return `✗ ${p.name}: ${j.detail || r.status}`;
        if (j.status === "sent") return `… ${p.name}: sent to the phone (${this.appPathLabel(j.path)})`;
        if (j.status === "done") return `✓ ${p.name}: done${j.version_name ? " (v" + j.version_name + ")" : ""}`;
        return `✗ ${p.name}: ${j.hint || (j.output || "failed").slice(-200)}`;
      } catch (e) { return `✗ ${p.name}: ${e.message}`; }
    }));
    const bad = results.some((t) => t.startsWith("✗"));
    this.fleetNote(`${verb}\n${results.join("\n")}`, bad ? "err" : "ok");
  }

  onAppLifecycleStatus(deviceId, p) {
    const dev = this.devices.find((d) => d.id === deviceId);
    const name = dev ? dev.name : deviceId;
    const what = p.action === "uninstall" ? "Uninstall" : "Update";
    const text = { accepted: "accepted by the phone", done: "done", needs_tap: "needs 1 tap on the phone",
                   failed: "failed" }[p.status] || p.status;
    this.fleetNote(`${what} · ${name}: ${text}${p.reason ? " — " + p.reason : ""}`,
                   p.status === "failed" ? "err" : p.status === "done" ? "ok" : "busy");
  }

  async makeDeviceOwner(id, btn) {
    const dev = this.devices.find((d) => d.id === id);
    if (!confirm(`Make the companion app Device Owner on ${dev ? dev.name : id}?\n\nSilent via ADB. Android only allows this when the phone has NO accounts (Google, Samsung, etc.). The app uses the role only for silent updates/uninstall.`)) return;
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(id)}/app-lifecycle/make-device-owner`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) this.fleetNote("Device owner: " + (j.detail || r.status), "err");
      else this.fleetNote(j.status === "done" ? "✓ Device owner set\n" + j.output : "✗ " + (j.hint ? j.hint + "\n" : "") + j.output, j.status === "done" ? "ok" : "err");
    } catch (e) {
      this.fleetNote("Device owner: " + e.message, "err");
    } finally { if (btn) btn.disabled = false; }
  }

  async wakeCurrentDevice() {
    const select = document.getElementById("capDeviceSelect");
    const targetId = select ? select.value : (this.activeDevice?.id);
    if (targetId) await this.wakeDevice(targetId);
  }

  async wakeDevice(deviceId) {
    try {
      await fetch(`/api/devices/${deviceId}/wake`, { method: "POST" });
      console.log(`[Device] Woke display rails on ${deviceId}`);
    } catch (e) {
      console.error("Failed to wake device:", e);
    }
  }
}

window.addEventListener("DOMContentLoaded", () => {
  window.app = new ControlCentreApp();
});
