"""Benchmark the live mirror / camera stream and USB input latency against a running server.

    python tools/bench_stream.py <device_id> --seconds 300 [--mode mirror|camera] [--scroll] [--input-probe]

Connects to /ws/dashboard like a browser that can decode H.264, starts the stream and prints: frames per
10 s window, frame-gap median/p95/max, KB/s, and every 30 s the phone's battery + thermal-zone temperatures.
--scroll       swipes up/down once a second (open a scrollable screen such as Settings first) to create motion.
--input-probe  times 20 VOLUME_UP/VOLUME_DOWN pairs through the REST input route (net volume change: none).
Read-only for the phone apart from those swipes/keys. Needs the server's own venv (websockets).
"""
import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ADB_PATH  # noqa: E402

ZONES = ("msm_therm", "tsens_tz_sensor1", "tsens_tz_sensor9")


def phone_temps(serial: str) -> str:
    script = ("dumpsys battery | grep temperature; for z in /sys/class/thermal/thermal_zone*; do "
              "t=$(cat $z/type 2>/dev/null); case $t in " + "|".join(ZONES) + ") echo $t $(cat $z/temp);; esac; done")
    try:
        out = subprocess.run([ADB_PATH, "-s", serial, "shell", script], capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        return f"(temps unavailable: {e})"
    return " | ".join(l.strip() for l in out.splitlines() if l.strip())


def post(base: str, path: str, body: dict) -> float:
    t = time.perf_counter()
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
    return (time.perf_counter() - t) * 1000


def input_probe(base: str, device_id: str) -> None:
    path = f"/api/devices/{device_id}/action/key"
    post(base, path, {"key": "VOLUME_UP"})       # warm-up (may start a control session)
    post(base, path, {"key": "VOLUME_DOWN"})
    lat = []
    for _ in range(20):
        lat.append(post(base, path, {"key": "VOLUME_UP"}))
        lat.append(post(base, path, {"key": "VOLUME_DOWN"}))
    lat.sort()
    print(f"input: {len(lat)} keys  median {statistics.median(lat):.1f} ms  p95 {lat[int(len(lat) * .95) - 1]:.1f} ms"
          f"  max {lat[-1]:.1f} ms")


async def bench(args) -> None:
    import websockets
    base = f"http://{args.host}"
    serial = json.load(urllib.request.urlopen(f"{base}/api/devices/{args.device_id}", timeout=10))["serial"]
    stamps, sizes, temps = [], [], [(0, phone_temps(serial))]
    seen_pts = set()      # a resync replays cached packets: count each frame once
    async with websockets.connect(f"ws://{args.host}/ws/dashboard", max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "CLIENT_CAPS", "payload": {"h264": True}}))
        start = {"mirror": {"type": "START_SCREEN_STREAM", "device_id": args.device_id},
                 "camera": {"type": "START_CAMERA_STREAM", "device_id": args.device_id, "payload": {"lens": "back"}}}
        await ws.send(json.dumps(start[args.mode]))
        t0 = time.time()

        async def sample_temps():        # its own task: an adb call must never pause packet receiving
            while True:
                await asyncio.sleep(30)
                temps.append((round(time.time() - t0), await asyncio.to_thread(phone_temps, serial)))

        async def keep_scrolling():
            flip = False
            while True:
                await asyncio.sleep(1.0)
                y1, y2 = (700, 1700) if flip else (1700, 700)
                flip = not flip
                await ws.send(json.dumps({"type": "INPUT_SWIPE", "device_id": args.device_id,
                                          "payload": {"x1": 540, "y1": y1, "x2": 540, "y2": y2, "duration_ms": 250}}))

        helpers = [asyncio.create_task(sample_temps())]
        if args.scroll:
            helpers.append(asyncio.create_task(keep_scrolling()))
        while time.time() - t0 < args.seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                raw = None
            if isinstance(raw, (bytes, bytearray)):
                if raw[0] in (2, 3):                       # key / delta video packet
                    n = raw[1]
                    pts = (raw[2:2 + n], int.from_bytes(raw[2 + n:10 + n], "big"))
                    if pts in seen_pts:
                        continue
                    seen_pts.add(pts)
                    stamps.append(time.time() - t0)
                    sizes.append(len(raw))
            elif raw:
                msg = json.loads(raw)
                if msg.get("type") in ("SCREEN_FRAME", "CAMERA_FRAME") and msg.get("device_id") == args.device_id:
                    stamps.append(time.time() - t0)
                    sizes.append(len(msg.get("payload", {}).get("data_base64", "")))
        for h in helpers:
            h.cancel()
        stop = {"mirror": "STOP_SCREEN_STREAM", "camera": "STOP_CAMERA_STREAM"}[args.mode]
        await ws.send(json.dumps({"type": stop, "device_id": args.device_id}))
    temps.append((round(time.time() - t0), phone_temps(serial)))

    dur = args.seconds
    print(f"{args.mode}: {len(stamps)} frames in {dur:.0f} s = {len(stamps) / dur:.1f} fps")
    if stamps:
        windows = [sum(1 for s in stamps if i <= s < i + 10) / 10 for i in range(0, int(dur), 10)]
        print("fps per 10 s window:", " ".join(f"{w:.1f}" for w in windows))
        print(f"lowest window {min(windows):.1f} fps")
        gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
        if gaps:
            print(f"frame gap ms: median {statistics.median(gaps) * 1000:.0f}  p95 {gaps[int(len(gaps) * .95) - 1] * 1000:.0f}"
                  f"  max {gaps[-1] * 1000:.0f}")
            worst = sorted(((b - a, a) for a, b in zip(stamps, stamps[1:])), reverse=True)[:5]
            print("largest gaps (ms @ s):", "  ".join(f"{g * 1000:.0f}@{at:.1f}" for g, at in worst))
        print(f"throughput {sum(sizes) / dur / 1024:.0f} KB/s")
    for t, s in temps:
        print(f"t={t:>4}s  {s}")
    if args.input_probe:
        input_probe(base, args.device_id)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("device_id")
    p.add_argument("--seconds", type=float, default=300)
    p.add_argument("--mode", choices=("mirror", "camera"), default="mirror")
    p.add_argument("--scroll", action="store_true")
    p.add_argument("--input-probe", action="store_true")
    p.add_argument("--host", default="127.0.0.1:8000")
    asyncio.run(bench(p.parse_args()))


if __name__ == "__main__":
    main()
