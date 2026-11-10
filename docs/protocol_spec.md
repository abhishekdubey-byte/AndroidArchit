# Protocol Specification: Android Control Centre (ACC-v1)

This document standardizes all WebSocket and REST communication messages exchanged between:
1. Android Companion Devices <--> Central Laptop Server
2. Web Dashboard <--> Central Laptop Server

---

## 1. Frame Envelope

All WebSocket messages use a standardized JSON wrapper:

```json
{
  "type": "MESSAGE_TYPE",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "timestamp": 1730553600000,
  "payload": {}
}
```

---

## 2. Telemetry & Heartbeat Messages

### `DEVICE_HELLO` (Device -> Server)
Sent upon initial WebSocket connection or device detection:
```json
{
  "type": "DEVICE_HELLO",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "model": "RMX3031",
    "brand": "realme",
    "android_version": "13",
    "display": {"width": 1080, "height": 2400, "density": 480},
    "capabilities": ["screen_stream", "touch", "camera", "files", "accessibility"]
  }
}
```

### `TELEMETRY_UPDATE` (Device/Bridge -> Server -> Dashboard)
Broadcast periodically (every 3-5 seconds):
```json
{
  "type": "TELEMETRY_UPDATE",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "battery": {
      "level": 51,
      "charging": true,
      "temperature": 31.8
    },
    "storage": {
      "total_gb": 108.0,
      "used_gb": 76.0,
      "free_gb": 32.0,
      "percent": 71
    },
    "network": {
      "type": "wifi",
      "connected": true,
      "ip": "192.168.1.45"
    },
    "current_app": {
      "package_name": "com.whatsapp",
      "activity_name": "com.whatsapp.HomeActivity"
    }
  }
}
```

---

## 3. Remote Control & Screen Streaming

### `SCREEN_FRAME` (Server -> Dashboard)
High-frequency binary or base64 JPEG/WebP frame:
```json
{
  "type": "SCREEN_FRAME",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "format": "jpeg",
    "width": 540,
    "height": 1200,
    "data_base64": "/9j/4AAQSkZJRg..."
  }
}
```

### `INPUT_TOUCH` (Dashboard -> Server -> Device)
```json
{
  "type": "INPUT_TOUCH",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "action": "tap",
    "x": 540,
    "y": 1200
  }
}
```

### `INPUT_SWIPE` (Dashboard -> Server -> Device)
```json
{
  "type": "INPUT_SWIPE",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "x1": 540, "y1": 1800,
    "x2": 540, "y2": 400,
    "duration_ms": 300
  }
}
```

### `INPUT_KEY` (Dashboard -> Server -> Device)
```json
{
  "type": "INPUT_KEY",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "key": "BACK" // "HOME", "RECENTS", "POWER", "ENTER"
  }
}
```

### `INPUT_TEXT` (Dashboard -> Server -> Device)
```json
{
  "type": "INPUT_TEXT",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "text": "Hello from my laptop!"
  }
}
```

---

## 4. AI Task Operator Protocol

### `TASK_SUBMIT` (Dashboard -> Server)
```json
{
  "type": "TASK_SUBMIT",
  "payload": {
    "target_device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
    "instruction": "Open WhatsApp, find conversation with Rahul, read latest message, draft reply"
  }
}
```

### `TASK_PLAN_UPDATE` (Server -> Dashboard)
```json
{
  "type": "TASK_PLAN_UPDATE",
  "payload": {
    "task_id": "task_928102",
    "status": "executing",
    "steps": [
      {"id": 1, "description": "Open WhatsApp application", "status": "done"},
      {"id": 2, "description": "Locate conversation with Rahul", "status": "done"},
      {"id": 3, "description": "Read latest incoming message", "status": "done"},
      {"id": 4, "description": "Draft professional response", "status": "done"},
      {"id": 5, "description": "Send reply to Rahul", "status": "waiting_approval"}
    ]
  }
}
```

### `APPROVAL_REQUIRED` (Server -> Dashboard)
```json
{
  "type": "APPROVAL_REQUIRED",
  "payload": {
    "task_id": "task_928102",
    "step_id": 5,
    "action_type": "send_message",
    "recipient": "Rahul",
    "proposed_content": "Thank you for the update. I'll review the details and get back to you shortly.",
    "warning": "This will send a live WhatsApp message from your connected device."
  }
}
```

### `APPROVAL_RESPONSE` (Dashboard -> Server)
```json
{
  "type": "APPROVAL_RESPONSE",
  "payload": {
    "task_id": "task_928102",
    "decision": "approved", // "rejected", "edited"
    "modified_content": null
  }
}
```

---

## 4. Telephony & Bidirectional Call Audio Messages

### `INCOMING_CALL` (Device / Server -> Dashboard)
Broadcast when an incoming call is detected on Android (mCallState = 1 / RINGING).
```json
{
  "type": "INCOMING_CALL",
  "device_id": "realme_rmx3031_192.168.1.6:5555",
  "payload": {
    "phone_number": "+918269220295",
    "caller_name": "Bro Code",
    "state": "ringing"
  }
}
```

### `ACCEPT_CALL` (Dashboard -> Server -> Device)
Dispatched when user clicks "Receive & Speak via Laptop" on dashboard.
Answers call via TelecomManager / ADB keyevent 5 (CALL), activates speakerphone, and starts bidirectional audio.
```json
{
  "type": "ACCEPT_CALL",
  "device_id": "realme_rmx3031_192.168.1.6:5555"
}
```

### `CALL_ACTIVE` (Server -> Dashboard)
Broadcast when call is answered and active (mCallState = 2 / OFFHOOK).
Transitions dashboard to active call mode with duration timer and live audio waveforms.
```json
{
  "type": "CALL_ACTIVE",
  "device_id": "realme_rmx3031_192.168.1.6:5555",
  "payload": {
    "phone_number": "+918269220295",
    "state": "active"
  }
}
```

### `END_CALL` (Dashboard -> Server -> Device)
Dispatched when user clicks "End Call" or "Decline". Hangs up call via TelecomManager / ADB keyevent 6 (ENDCALL).
```json
{
  "type": "END_CALL",
  "device_id": "realme_rmx3031_192.168.1.6:5555"
}
```

### `CALL_ENDED` (Server -> Dashboard)
Broadcast when call disconnects or is declined (mCallState = 0 / IDLE).
```json
{
  "type": "CALL_ENDED",
  "device_id": "realme_rmx3031_192.168.1.6:5555",
  "payload": {
    "state": "idle"
  }
}
```

### `LAPTOP_MIC_AUDIO` (Dashboard -> Server -> Device Companion)
Transmits laptop microphone audio chunks (16kHz 16-bit PCM Mono) to phone during call, played via AudioTrack USAGE_VOICE_COMMUNICATION so caller hears the laptop user.
```json
{
  "type": "LAPTOP_MIC_AUDIO",
  "device_id": "realme_rmx3031_192.168.1.6:5555",
  "payload": {
    "pcm_base64": "AP8B/wH/Af8B..."
  }
}
```

### `PHONE_AUDIO_CHUNK` (Device Companion -> Server -> Dashboard)
Transmits caller audio captured by Android AudioRecord (16kHz 16-bit PCM Mono) to laptop speakers via Web Audio API.
```json
{
  "type": "PHONE_AUDIO_CHUNK",
  "device_id": "realme_rmx3031_192.168.1.6:5555",
  "payload": {
    "pcm_base64": "AP8B/wH/Af8B..."
  }
}
```

---

## 7. Headless Direct-Sensor Camera & Video Protocol (Zero UI / Low-Power)

Eliminates the stock camera application (`com.oplus.camera`), viewfinder rendering, and display wakeups. Directly addresses the Camera2 HAL sensor via `HeadlessCameraActivity` and `BackgroundCameraService`.

### `CAPTURE_PHOTO` (Dashboard / AI Operator -> Server -> Companion)
Requests an immediate still JPEG from either the rear or front hardware sensor without displaying any camera UI.
```json
{
  "type": "CAPTURE_PHOTO",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "lens": "back"
  }
}
```

### `PHOTO_CAPTURED` (Companion -> Server -> Dashboard)
Broadcast when sensor snapshot is saved to disk and buffered.
```json
{
  "type": "PHOTO_CAPTURED",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "success": true,
    "file_path": "/storage/emulated/0/Download/capture_silent_1793636325210.jpg",
    "filename": "capture_silent_1793636326.jpg",
    "url": "/storage/downloads/capture_silent_1793636326.jpg",
    "lens": "back"
  }
}
```

### `RECORD_VIDEO` (Dashboard / AI Operator -> Server -> Companion)
Commands the phone to record a silent H.264/AAC MP4 video directly from either the rear or front camera sensor for a specified duration (e.g., 10s, 15s, 25s, 30s, 45s, 60s / 1 min).
```json
{
  "type": "RECORD_VIDEO",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "lens": "front",
    "duration_sec": 15
  }
}
```

### `VIDEO_RECORDED` (Companion -> Server -> Dashboard)
Broadcast when the hardware video session closes and the file is synchronized.
```json
{
  "type": "VIDEO_RECORDED",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "success": true,
    "file_path": "/storage/emulated/0/Download/video_silent_1793637688280.mp4",
    "filename": "video_silent_1793637700.mp4",
    "url": "/storage/downloads/video_silent_1793637700.mp4",
    "duration_sec": 15,
    "lens": "front"
  }
}
```

### Zero-Residual Phone Storage Policy
To preserve phone storage and privacy:
1. Videos and photos recorded headlessly are immediately pulled into laptop storage (`server/storage/downloads/`).
2. As soon as the transfer succeeds, the server issues an immediate file purge (`rm -f <remote_path>`) on the Android device.
3. The Companion App also runs an internal 25-second watchdog coroutine to purge any temporary recording files from the phone if not already removed.
4. **Result**: The recording is saved strictly on the laptop, and 0 bytes are left on the Android smartphone.

---

## 9. Centralized Remote Diagnostics & Error Telemetry

### `DEVICE_ERROR` (Companion -> Server -> Dashboard)
Transmitted whenever an unhandled exception, hardware HAL failure, or permission fault occurs on an enrolled device. Prevents disruptive Android dialogs or popups by streaming diagnostics directly to the laptop operator.

```json
{
  "type": "DEVICE_ERROR",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "timestamp": 1793640000000,
  "module": "camera_hardware",
  "operation": "record_video",
  "error_code": "CAMERA_IN_USE_BACKGROUND",
  "description": "Camera sensor 0 was temporarily held by a background scanner process.",
  "suggested_resolution": "Ensure sensor rails are awake or retry with front camera.",
  "device_info": {
    "brand": "realme",
    "manufacturer": "realme",
    "model": "RMX3031",
    "android_version": "13",
    "sdk_int": 33
  },
  "stack_trace": "java.lang.IllegalStateException: Camera in use\n\tat com.operator.companion.service.BackgroundCameraService.recordVideo(BackgroundCameraService.kt:281)"
}
```

---

## 10. Device Capability Matrix Messages

### `GET_CAPABILITIES` (Dashboard/Server -> Companion)
Requests comprehensive capability inspection from the companion app.
```json
{
  "type": "GET_CAPABILITIES",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS"
}
```

### `DEVICE_CAPABILITIES` (Companion -> Server -> Dashboard)
Broadcasts detected hardware characteristics, camera levels, permissions, and lockscreen compatibility.
```json
{
  "type": "DEVICE_CAPABILITIES",
  "device_id": "realme_rmx3031_FMOVVW6HMRQ4C6RS",
  "payload": {
    "device_info": {
      "manufacturer": "realme",
      "model": "RMX3031",
      "android_version": "13",
      "sdk_int": 33
    },
    "camera_capabilities": {
      "rear_hardware_level": "FULL",
      "front_hardware_level": "LIMITED",
      "max_photo_resolution": "1920x1080",
      "max_video_resolution": "1280x720"
    },
    "power_and_lockscreen": {
      "screen_interactive": false,
      "is_device_locked": true,
      "headless_lockscreen_supported": true
    },
    "permissions": {
      "camera": true,
      "audio": true,
      "phone_state": true,
      "answer_calls": true,
      "accessibility_service": true
    }
  }
}
```



