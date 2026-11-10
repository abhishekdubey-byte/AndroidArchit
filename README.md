# Android Control Centre & Multi-Device AI Operator

An enterprise-grade, standalone command-and-control platform and autonomous AI operator for Android devices, operated directly from your Linux/Debian laptop.

![Dashboard Preview](https://img.shields.io/badge/Status-Live%20%26%20Active-brightgreen)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI%20%2B%20WebSockets-blue)
![Android](https://img.shields.io/badge/Android-13%20%7C%20Realme%20X7%20Max-success)
![AI Operator](https://img.shields.io/badge/AI%20Agent-Autonomous%20with%20Approval%20Guard-purple)

---

## 🌟 Key Features

1. **Dual-Transport Architecture**:
   - **ADB Bridge Transport (Zero-Install)**: Instantly detects connected USB / Wi-Fi devices, streams screen frames, injects mouse-to-touch taps/swipes, inspects accessibility UI trees via `uiautomator`, and launches applications without needing any app pre-installed.
   - **Native Android Companion APK (Kotlin + Jetpack Compose)**: Foreground service with `AccessibilityService` for deep app automation, `MediaProjection` screen streaming, `CameraX` photo capture, and `AudioRecord`.
2. **Multi-Device Fleet Registry**:
   - Simultaneous tracking of multiple connected phones and emulators.
   - Live telemetry: battery %, charging state, temperature, storage allocation (used/free), network, active foreground package.
3. **Live Screen Mirroring & Interactive Canvas**:
   - High-performance, low-latency live screen mirroring rendered on an HTML5 canvas.
   - Mouse click-to-touch, drag-to-swipe, and on-screen hardware buttons (`Back`, `Home`, `Recents`, `Power`).
   - Quick typing box to inject text into the phone directly from your laptop keyboard.
4. **Autonomous AI Task Operator with Human-in-the-Loop Safety**:
   - Accepts natural-language tasks (e.g., *"Open WhatsApp, check conversation with Rahul, draft reply"* or *"Capture photo with rear camera and transfer to laptop"*).
   - Decomposes instructions into discrete executable steps.
   - **Mandatory Approval Guard**: Pauses sensitive operations (sending messages, deleting files, purchasing/posting) and generates an interactive approval card on your dashboard for confirmation before transmission.
5. **Hands-Free Call Reception & Bidirectional Laptop Audio**:
   - **Incoming Call Detection**: Instant sub-second detection of incoming calls with Caller ID & phone number display.
   - **One-Click Answer from Laptop**: Accept incoming calls with the "Receive & Speak via Laptop" button; automatically enables speakerphone.
   - **Bidirectional Audio Bridge**: Speak directly into your laptop's microphone to talk to the caller while the caller's voice plays live out of your laptop speakers or headphones.
   - **In-Call Controls**: Live call timer, visual audio equalizer, microphone volume meter, mute microphone toggle, and one-click call termination.
6. **Quick Social App Launcher**:
   - Instant launch for WhatsApp, Instagram, Chrome, Camera, YouTube, and Settings.
7. **Device File Manager (all Android versions, 5.0 → 15+)** — `http://localhost:8000/files.html`:
   - Browse internal storage, SD cards / USB-OTG volumes, quick-access folders (Camera, Screenshots, WhatsApp media…).
   - Download files (streamed) or whole folders / multi-selections (zip); upload files, folders and drag-and-drop with progress.
   - New folder, rename, copy/cut/paste, delete (volume roots and system paths are protected).
   - One tab per phone (`＋ New tab`) to work on several devices at the same time; auto-resumes when a device reconnects.
   - Pure ADB transport (no companion app or storage permission needed), with fallbacks for old toolbox shells (Android 5/6).
   - **Thumbnail grid + in-page viewer**: photos/videos preview in the folder; click to view full size, play/seek videos, ←/→ to browse. Nothing opens on the phone.
8. **Remote phones on any network** — `http://localhost:8000/pair.html`:
   - The companion app keeps an outbound, auto-reconnecting connection (foreground service, starts at boot, follows Wi-Fi ↔ mobile-data changes), so phones on other networks work without ADB.
   - Pair by **Pair via USB** (one click) or by scanning the QR code (the phone asks for confirmation).
   - Remote control (screen view ~1 fps on Android 11+, touch/keys/typing, app list & launcher, AI operator, camera) needs the app's **Accessibility service** switched on once per phone. Over USB everything works as before.
   - Screen mirroring has an on/off switch on the dashboard (off by default to save the phone's battery).
   - Same Wi-Fi works out of the box. For other networks use the **free Cloudflare relay** (`cloud-relay/`, Workers + Durable Objects): laptop and phones both dial out to `https://acc-relay.<you>.workers.dev`, so it works from any network on either side, with a permanent address. Alternatively expose port 8000 with a tunnel/port-forward and paste its URL on the pair page.

   **Deploy the relay (one time):**
   ```bash
   cd cloud-relay
   npx wrangler login                      # approve in the browser (free Cloudflare account)
   npx wrangler deploy                     # prints https://acc-relay.<you>.workers.dev
   python3 -c "import secrets;print(secrets.token_urlsafe(32))" | npx wrangler secret put HUB_KEY
   ```
   Then on `/pair.html` → *Cloudflare relay*: paste the URL and the same hub key, Save, and re-pair phones.
   The relay checks phone tokens itself (the laptop pushes SHA-256 hashes), caps message size, and drops phones if the laptop stops sending heartbeats. Local testing: `npx wrangler dev --ip 0.0.0.0 --port 8787` with `HUB_KEY=…` in `cloud-relay/.dev.vars`.
   - Security: phones get a device token valid only for `/ws/device`; each phone's identity is bound to a secret issued on first connect; other machines need the dashboard token (`?token=`); cross-site and DNS-rebinding requests are refused; "Change tokens" disconnects all remote sessions.

---

## 🚀 Quick Start

### 1. Central Server & Web Dashboard
The server runs on FastAPI and WebSockets, hosting the modern web control center at `http://localhost:8000`:

```bash
# Activate Python virtual environment and start server
cd /home/mrityunjay/AndroidArchit/server
source .venv/bin/activate
python run_server.py
```

Open your browser to: **`http://localhost:8000`**

### 2. Connected Device Status
Currently connected and live on your system:
- **Device**: Realme RMX3031 (Realme X7 Max 5G)
- **Android Version**: 13
- **Resolution**: 1080 x 2400 @ 480 dpi
- **Transport**: Low-latency ADB Bridge (Authorized via USB Debugging)

---

## 📡 REST API & WebSocket Protocol

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/devices` | `GET` | List all registered devices and live telemetry |
| `/api/devices/{id}/screen` | `GET` | Fetch real-time screen snapshot (JPEG) |
| `/api/devices/{id}/action/touch` | `POST` | Inject tap or swipe gesture |
| `/api/devices/{id}/action/key` | `POST` | Send hardware key (`BACK`, `HOME`, `RECENTS`, `POWER`) |
| `/api/devices/{id}/action/text` | `POST` | Inject text into active input field |
| `/api/devices/{id}/apps` | `GET` | List installed applications |
| `/api/devices/{id}/apps/launch` | `POST` | Launch an application by package name |
| `/api/devices/{id}/files` | `GET` | Browse device storage (`/sdcard/Download`) |
| `/api/devices/{id}/call/state` | `GET` | Get live telephony status (`0=idle`, `1=ringing`, `2=active`) |
| `/api/devices/{id}/call/answer` | `POST` | Answer incoming phone call and activate speakerphone |
| `/api/devices/{id}/call/end` | `POST` | Hang up active call or reject incoming call |
| `/api/devices/{id}/call/simulate_incoming` | `POST` | Simulate an incoming call for testing/demo |
| `/api/devices/{id}/fs/roots` | `GET` | Storage volumes, quick-access folders, free space |
| `/api/devices/{id}/fs/list?path=` | `GET` | List a folder (any path readable by adb shell) |
| `/api/devices/{id}/fs/download?path=[&path=…]` | `GET` | Stream a file, or zip folders / multiple paths |
| `/api/devices/{id}/fs/upload` | `POST` | Multipart upload (`dir`, `file`, optional `relpath`, `overwrite`) |
| `/api/devices/{id}/fs/mkdir` · `rename` · `delete` · `transfer` | `POST` | Folder create, rename, delete, copy/move |
| `/api/devices/{id}/fs/thumb?path=&mtime=&size=` | `GET` | JPEG thumbnail (laptop-side for ADB, phone-side for remote) |
| `/api/remote/pairing` · `config` · `token/rotate` · `provision/{id}` · `forget/{relay_id}` | `GET/POST` | Remote pairing management |
| `/ws/device` | `WebSocket` | Companion app relay connection (device token) |
| `/api/tasks` | `POST` | Submit natural-language task to the AI Operator |
| `/api/tasks/{id}/approve` | `POST` | Approve, reject, or modify sensitive action |
| `/ws/dashboard` | `WebSocket` | Real-time bi-directional streaming, telephony & audio bridge |

---

## 📁 Repository Structure

```
AndroidArchit/
├── README.md                      # Project documentation
├── docs/
│   ├── system_architecture.md     # Full architectural specification & diagrams
│   └── protocol_spec.md           # ACC-v1 WebSocket frame and JSON schema contracts
├── server/                        # Central Laptop Control Server
│   ├── app/
│   │   ├── main.py                # FastAPI app & WebSocket endpoints
│   │   ├── config.py              # Storage and network configurations
│   │   ├── models/schemas.py      # Pydantic schemas for devices & tasks
│   │   ├── services/
│   │   │   ├── adb_bridge.py      # ADB transport driver (screen, input, UI tree)
│   │   │   ├── device_manager.py  # Fleet registry & telemetry poll loop
│   │   │   └── ai_agent.py        # Autonomous multi-step task operator & approval guard
│   │   ├── websocket/hub.py       # High-speed frame streaming hub
│   │   └── api/routes.py          # REST endpoints
│   ├── static/                    # Modern Web Control Centre Dashboard
│   │   ├── index.html             # Glassmorphism dark mode UI
│   │   ├── css/style.css          # Design system, bezel & animations
│   │   └── js/app.js              # Canvas touch mapper, AI operator console
│   └── run_server.py              # Server launcher
└── android-companion/             # Standalone Android Companion Application
    ├── build.gradle.kts           # Root gradle configuration
    ├── settings.gradle.kts        # Module definitions
    └── app/
        ├── build.gradle.kts       # Jetpack Compose, CameraX, OkHttp
        └── src/main/
            ├── AndroidManifest.xml# Permissions & Accessibility declarations
            ├── res/xml/accessibility_service_config.xml
            └── java/com/operator/companion/
                ├── MainActivity.kt# Compose UI with pairing & permission toggles
                ├── service/CompanionAccessibilityService.kt # Accessibility driver
                └── network/CompanionWebSocketClient.kt      # WebSocket client
```
