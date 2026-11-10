import asyncio
import json
import urllib.request
import websockets
from pathlib import Path

BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/dashboard"

async def run_suite():
    print("=" * 65)
    print("  Cross-Device, Lockscreen & Remote Diagnostics Test Suite")
    print("=" * 65)

    # 1. Device Fleet Discovery
    print("\n[Test 1] Querying Connected Device Fleet (/api/devices)...")
    req = urllib.request.Request(f"{BASE_URL}/api/devices")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        devices = json.loads(resp.read().decode())
    
    assert len(devices) > 0, "No devices discovered"
    print(f"  -> Discovered {len(devices)} device(s):")
    for d in devices:
        print(f"     * {d['name']} | OEM: {d['manufacturer']} | Android {d['android_version']} (API {d['sdk_int']}) | Lockscreen Supported: {d['power_and_lockscreen'].get('headless_lockscreen_supported')}")

    target_dev = devices[0]
    dev_id = target_dev["id"]

    # 2. Device Capabilities Matrix Endpoint
    print(f"\n[Test 2] Querying Device Capabilities Matrix for {dev_id}...")
    req = urllib.request.Request(f"{BASE_URL}/api/devices/{dev_id}/capabilities")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        caps = json.loads(resp.read().decode())
    
    print(f"  -> Capabilities verified: OEM={caps['manufacturer']}, Model={caps['model']}, Android={caps['android_version']}")
    assert "lockscreen_capture" in caps["supported_features"], "lockscreen_capture not in supported features"
    print("  -> lockscreen_capture feature: ACTIVE")

    # 3. Wake Display Rails API
    print(f"\n[Test 3] Testing Wake Display Rails API (/api/devices/{dev_id}/wake)...")
    req = urllib.request.Request(f"{BASE_URL}/api/devices/{dev_id}/wake", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        wake_res = json.loads(resp.read().decode())
        print(f"  -> {wake_res['message']}")

    # 4. Headless Photo Capture directly to Laptop
    print(f"\n[Test 4] Testing Headless Photo Capture while Screen is Locked...")
    photo_url = f"{BASE_URL}/api/devices/{dev_id}/camera/photo?lens=back"
    req = urllib.request.Request(photo_url, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        photo_res = json.loads(resp.read().decode())
        assert photo_res["success"] is True
        local_path = Path(photo_res["local_path"])
        assert local_path.exists() and local_path.stat().st_size > 0
        print(f"  -> Photo successfully saved to laptop: {local_path.name} ({local_path.stat().st_size} bytes)")

    # 5. Remote Diagnostics Telemetry Dispatch & Verification
    print(f"\n[Test 5] Testing Remote Diagnostic Dispatch & Ingestion...")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv() # Initial state
        test_err = {
            "type": "DEVICE_ERROR",
            "device_id": dev_id,
            "timestamp": 1793640000000,
            "module": "camera_hardware",
            "operation": "open_camera",
            "error_code": "CAMERA_IN_USE_BACKGROUND",
            "description": "Camera sensor 0 was temporarily held by a background scanner process.",
            "suggested_resolution": "Ensure sensor rails are awake or retry with front camera.",
            "device_info": {
                "brand": caps["brand"],
                "manufacturer": caps["manufacturer"],
                "model": caps["model"],
                "android_version": caps["android_version"],
                "sdk_int": caps["sdk_int"]
            },
            "stack_trace": "java.lang.IllegalStateException: Camera in use\n\tat com.operator.companion.service.BackgroundCameraService"
        }
        await ws.send(json.dumps(test_err))
        
        # Verify broadcast
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        broadcast_data = json.loads(msg)
        assert broadcast_data["type"] == "DEVICE_ERROR"
        assert broadcast_data["payload"]["error_code"] == "CAMERA_IN_USE_BACKGROUND"
        print(f"  -> Received real-time broadcast: [{broadcast_data['payload']['error_code']}] {broadcast_data['payload']['description']}")

    # Verify REST endpoint persistence
    req = urllib.request.Request(f"{BASE_URL}/api/diagnostics")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        diag_res = json.loads(resp.read().decode())
        assert diag_res["total_reports"] >= 1
        sample = diag_res["reports"][0]
        print(f"  -> Verified in /api/diagnostics: [{sample['device_id']}] {sample['module']} -> {sample['error_code']}")
        print(f"  -> Suggested Resolution: {sample['suggested_resolution']}")

    req = urllib.request.Request(f"{BASE_URL}/api/devices/{dev_id}/diagnostics")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        dev_diag_res = json.loads(resp.read().decode())
        assert len(dev_diag_res["diagnostics"]) >= 1
        print(f"  -> Verified in /api/devices/{dev_id}/diagnostics: {len(dev_diag_res['diagnostics'])} report(s)")

    print("\n" + "=" * 65)
    print("  >>> ALL CROSS-DEVICE, LOCKSCREEN & DIAGNOSTIC TESTS PASSED! <<<")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(run_suite())
