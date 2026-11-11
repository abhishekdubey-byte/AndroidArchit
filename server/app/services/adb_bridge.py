import shlex
import asyncio
import base64
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

from ..config import ADB_PATH, SCREENSHOT_DIR, DOWNLOADS_DIR, device_capture_dir
from ..models.schemas import Device, DisplayInfo, BatteryInfo, StorageInfo, NetworkInfo, AppInfo, UINode

KEY_MAP = {
    "HOME": 3,
    "BACK": 4,
    "RECENTS": 187,
    "APP_SWITCH": 187,
    "POWER": 26,
    "ENTER": 66,
    "TAB": 61,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "WAKEUP": 224,
    "SLEEP": 223
}

class UnsupportedText(Exception):
    """`adb shell input text` can only type printable ASCII; anything else would be silently dropped."""


class UiDumpUnavailable(Exception):
    """uiautomator could not read the screen (it waits for an idle screen, which animations never give)."""


def adb_typable(text: str) -> bool:
    """True iff every character can be typed by `adb shell input text` (printable ASCII or newline)."""
    return all(c == "\n" or " " <= c <= "~" for c in text)


LAUNCHER_QUERY = ("cmd package query-activities --brief -a android.intent.action.MAIN "
                  "-c android.intent.category.LAUNCHER")
_LAUNCHER_LINE = re.compile(r"^([A-Za-z][\w.]*)/\S+$")


def parse_launcher_packages(output: str) -> List[str]:
    """Packages from `cmd package query-activities --brief` ('pkg/.Activity' lines), de-duplicated, in order."""
    seen: List[str] = []
    for line in (output or "").splitlines():
        m = _LAUNCHER_LINE.match(line.strip())
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


class ADBBridge:
    UI_DUMP_TIMEOUT = 12.0  # seconds; uiautomator never returns on animated screens

    def __init__(self, adb_path: str = ADB_PATH):
        self.adb_path = adb_path
        self._screen_cache: Dict[str, Tuple[float, bytes, str]] = {}

    async def ensure_adb_server(self):
        """Start the adb daemon detached (own session, no inherited pipes). If the first adb call of a boot
        spawns the daemon itself, the daemon can keep our stdout pipe open and that call never returns —
        which froze server startup on the first launch after a reboot."""
        if getattr(self, "_adb_server_ok", False):
            return
        try:
            p = await asyncio.create_subprocess_exec(
                self.adb_path, "start-server", stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            await asyncio.wait_for(p.wait(), 20)
            self._adb_server_ok = True
        except Exception as e:
            print(f"[ADB] could not start the adb server: {e}")

    async def run_command(self, *args: str) -> Tuple[int, str, str]:
        await self.ensure_adb_server()
        cmd = [self.adb_path] + list(args)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()   # a cancelled/timed-out call must not leave adb running
            raise
        return process.returncode or 0, stdout.decode('utf-8', errors='ignore'), stderr.decode('utf-8', errors='ignore')

    async def run_device_shell(self, serial: str, command: str) -> str:
        code, out, _ = await self.run_command("-s", serial, "shell", command)
        return out.strip()

    async def list_attached_devices(self) -> List[Dict[str, str]]:
        code, out, _ = await self.run_command("devices", "-l")
        devices = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                model = "Android Device"
                product = "generic"
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p.split(":", 1)[1]
                    elif p.startswith("product:"):
                        product = p.split(":", 1)[1]
                devices.append({
                    "serial": serial,
                    "model": model,
                    "product": product
                })
        return devices

    async def get_device_details(self, serial: str) -> Device:
        # Query basic hardware and OS specs
        props_script = """
        getprop ro.product.model
        echo "---"
        getprop ro.product.brand
        echo "---"
        getprop ro.build.version.release
        echo "---"
        wm size
        echo "---"
        wm density
        echo "---"
        getprop ro.product.manufacturer
        echo "---"
        getprop ro.build.version.sdk
        """
        output = await self.run_device_shell(serial, props_script)
        parts = [p.strip() for p in output.split("---")]
        if not (parts and parts[0]) or len(parts) < 2 or not parts[1]:
            # The phone didn't answer (still authorising / adb restarting). Don't invent a placeholder
            # "Android Device": it gets a different id and shows up as a duplicate with fake battery/storage.
            raise RuntimeError(f"no device properties from {serial} yet")

        model = parts[0] if len(parts) > 0 and parts[0] else "Android Device"
        brand = parts[1] if len(parts) > 1 and parts[1] else "Android"
        android_version = parts[2] if len(parts) > 2 and parts[2] else "13"
        
        # Parse screen size and density
        width, height, density = 1080, 2400, 480
        if len(parts) > 3:
            match = re.search(r'Physical size:\s*(\d+)x(\d+)', parts[3])
            if match:
                width, height = int(match.group(1)), int(match.group(2))
        if len(parts) > 4:
            match = re.search(r'Physical density:\s*(\d+)', parts[4])
            if match:
                density = int(match.group(1))

        manufacturer = parts[5] if len(parts) > 5 and parts[5] else brand
        sdk_int = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 33

        # Battery info
        battery_out = await self.run_device_shell(serial, "dumpsys battery")
        level, charging, temp = 100, False, 25.0
        for b_line in battery_out.splitlines():
            b_line = b_line.strip()
            if b_line.startswith("level:"):
                try: level = int(b_line.split(":")[1].strip())
                except: pass
            elif b_line.startswith("USB powered:") or b_line.startswith("AC powered:"):
                if "true" in b_line:
                    charging = True
            elif b_line.startswith("temperature:"):
                try: temp = float(b_line.split(":")[1].strip()) / 10.0
                except: pass

        # Storage info
        df_out = await self.run_device_shell(serial, "df -h /data")
        total_gb, used_gb, free_gb, percent = 128.0, 64.0, 64.0, 50
        for d_line in df_out.splitlines():
            if "/data" in d_line:
                d_parts = d_line.split()
                if len(d_parts) >= 5:
                    def parse_size(s: str) -> float:
                        s = s.upper().replace('G', '').replace('M', '').replace('K', '')
                        try: return float(s)
                        except: return 0.0
                    total_gb = parse_size(d_parts[1])
                    used_gb = parse_size(d_parts[2])
                    free_gb = parse_size(d_parts[3])
                    try: percent = int(d_parts[4].replace('%', ''))
                    except: percent = int((used_gb / total_gb * 100) if total_gb > 0 else 50)

        # Active foreground package & lock state
        focus_out = await self.run_device_shell(serial, "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp|isStatusBarKeyguard'")
        active_pkg = "com.android.launcher"
        pkg_match = re.search(r'([a-zA-Z0-9_\.]+\/[a-zA-Z0-9_\.]+)', focus_out)
        if pkg_match:
            active_pkg = pkg_match.group(1).split('/')[0]

        power_out = await self.run_device_shell(serial, "dumpsys power | grep 'Display Power: state='")
        is_screen_on = "state=ON" in power_out

        dev_id = f"{brand.lower()}_{model.lower().replace(' ', '_')}_{serial}"
        friendly_name = f"{brand.capitalize()} {model} ({serial[:6]}...)"

        return Device(
            id=dev_id,
            serial=serial,
            name=friendly_name,
            model=model,
            brand=brand,
            manufacturer=manufacturer,
            android_version=android_version,
            sdk_int=sdk_int,
            display=DisplayInfo(width=width, height=height, density=density),
            battery=BatteryInfo(level=level, charging=charging, temperature=temp),
            storage=StorageInfo(total_gb=total_gb, used_gb=used_gb, free_gb=free_gb, percent=percent),
            network=NetworkInfo(type="usb/wifi", connected=True, ip="Connected via ADB"),
            transport="adb",
            status="online",
            active_package=active_pkg,
            power_and_lockscreen={
                "screen_interactive": is_screen_on,
                "is_device_locked": "isStatusBarKeyguard=true" in focus_out or "Keyguard" in focus_out,
                "headless_lockscreen_supported": True
            },
            capabilities=[
                "screen_mirror",
                "touch_injection",
                "keyboard_input",
                "app_management",
                "file_transfer",
                "ui_hierarchy_dump",
                "camera_capture",
                "audio_recording",
                "lockscreen_capture"
            ],
            last_seen=asyncio.get_event_loop().time()
        )

    async def capture_screen(self, serial: str, scale_width: int = 540, quality: int = 70) -> Tuple[bytes, str]:
        # Fast capture via exec-out screencap -p
        cmd = [self.adb_path, "-s", serial, "exec-out", "screencap", "-p"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        png_bytes, _ = await process.communicate()
        if not png_bytes or len(png_bytes) < 100:
            raise RuntimeError("Failed to capture screen from device")

        # Process with PIL in-memory to optimize size and latency
        loop = asyncio.get_event_loop()
        def process_image():
            img = Image.open(io.BytesIO(png_bytes))
            # Calculate scaled height maintaining aspect ratio
            w, h = img.size
            scaled_h = int(h * (scale_width / w))
            resized = img.resize((scale_width, scaled_h), Image.Resampling.BILINEAR)
            if resized.mode in ("RGBA", "P"):
                resized = resized.convert("RGB")
            out_buf = io.BytesIO()
            resized.save(out_buf, format="JPEG", quality=quality, optimize=True)
            jpeg_data = out_buf.getvalue()
            b64_str = base64.b64encode(jpeg_data).decode('ascii')
            return jpeg_data, b64_str

        jpeg_data, b64_str = await loop.run_in_executor(None, process_image)
        return jpeg_data, b64_str

    async def inject_tap(self, serial: str, x: int, y: int) -> bool:
        out = await self.run_device_shell(serial, f"input tap {x} {y}")
        return True

    async def inject_swipe(self, serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
        out = await self.run_device_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")
        return True

    async def inject_key(self, serial: str, key_name: str) -> bool:
        code = KEY_MAP.get(key_name.upper(), 4)
        out = await self.run_device_shell(serial, f"input keyevent {code}")
        return True

    async def inject_text(self, serial: str, text: str) -> bool:
        if not text:
            return True
        if not adb_typable(text):
            raise UnsupportedText("Non-English text and emoji need the companion app connected")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                words = line.split(" ")
                escaped_words = []
                for w in words:
                    # Escape all shell characters
                    safe_w = re.sub(r'([\\`"\$\!\&\;\(\)\<\>\*\?\|\'\#\~\[\]\{\}])', r'\\\1', w)
                    escaped_words.append(safe_w)
                safe_line = "%s".join(escaped_words)
                await self.run_device_shell(serial, f"input text {safe_line}")
            if i < len(lines) - 1:
                await self.inject_key(serial, "ENTER")
        return True

    async def launch_package(self, serial: str, package_name: str) -> bool:
        cmd = f"monkey -p {shlex.quote(package_name)} -c android.intent.category.LAUNCHER 1"
        out = await self.run_device_shell(serial, cmd)
        return "Events injected: 1" in out

    async def companion_installed(self, serial: str) -> bool:
        """True iff the Operator Companion app is installed for the phone's current user."""
        out = await self.run_device_shell(serial, "pm list packages com.operator.companion")
        return any(l.strip() == "package:com.operator.companion" for l in (out or "").splitlines())

    async def foreground_package(self, serial: str) -> Optional[str]:
        """Package of the window that has focus right now (None if it cannot be read)."""
        out = await self.run_device_shell(serial, "dumpsys window windows | grep -E 'mCurrentFocus'")
        m = re.search(r"mCurrentFocus=Window\{\S+ u\d+ ([A-Za-z][\w.]*)/", out or "")
        return m.group(1) if m else None

    async def get_installed_apps(self, serial: str) -> List[AppInfo]:
        third_party_out = await self.run_device_shell(serial, "pm list packages -3")
        third_party = {l.strip().split(":", 1)[1] for l in third_party_out.splitlines() if l.strip().startswith("package:")}
        # Everything with a home-screen icon, built-in apps (Settings, Camera…) included
        packages = parse_launcher_packages(await self.run_device_shell(serial, LAUNCHER_QUERY)) or sorted(third_party)

        friendly_map = {
            "com.whatsapp": "WhatsApp",
            "com.whatsapp.w4b": "WhatsApp Business",
            "com.instagram.android": "Instagram",
            "com.instagram.barcelona": "Threads",
            "com.android.chrome": "Google Chrome",
            "com.oplus.camera": "Camera",
            "com.android.camera": "Camera",
            "com.google.android.apps.photos": "Google Photos",
            "com.google.android.youtube": "YouTube",
            "com.google.android.apps.maps": "Google Maps",
            "com.android.settings": "Settings",
            "com.facebook.katana": "Facebook",
            "com.facebook.orca": "Messenger",
            "com.android.vending": "Play Store",
            "com.miui.securitycenter": "Security",
        }

        apps = []
        for pkg in sorted(packages):
            app_name = friendly_map.get(pkg, pkg.split('.')[-1].capitalize())
            apps.append(AppInfo(
                package_name=pkg,
                app_name=app_name,
                is_system=pkg not in third_party
            ))
        return apps

    async def dump_ui_hierarchy(self, serial: str) -> List[UINode]:
        xml_path = "/sdcard/window_dump.xml"
        # Remove the old dump first (a failed dump must not return a stale screen) and never wait forever:
        # uiautomator blocks until the screen is idle, which never happens on animations / playing video.
        idle_msg = ("The screen never went idle (animations or video), so it could not be read. "
                    "Turn on the companion app's Accessibility to read any screen.")
        try:
            out = await asyncio.wait_for(
                self.run_device_shell(serial, f"rm -f {xml_path}; uiautomator dump {xml_path}"), self.UI_DUMP_TIMEOUT)
        except asyncio.TimeoutError:
            await self.run_device_shell(serial, "pkill -f uiautomator 2>/dev/null; killall uiautomator 2>/dev/null")
            raise UiDumpUnavailable(idle_msg)
        if "ERROR" in (out or ""):
            raise UiDumpUnavailable(idle_msg)
        xml_content = await self.run_device_shell(serial, f"cat {xml_path} 2>/dev/null")

        if not xml_content or not xml_content.startswith("<?xml"):
            raise UiDumpUnavailable("The phone did not return a screen layout. Try again, or turn on the "
                                    "companion app's Accessibility.")

        def parse_node(elem: ET.Element, idx: int = 0) -> Optional[UINode]:
            bounds_str = elem.attrib.get('bounds', '[0,0][0,0]')
            match = re.findall(r'\[(\d+),(\d+)\]', bounds_str)
            bounds = [0, 0, 0, 0]
            if len(match) >= 2:
                bounds = [int(match[0][0]), int(match[0][1]), int(match[1][0]), int(match[1][1])]

            node = UINode(
                index=idx,
                text=elem.attrib.get('text', ''),
                resource_id=elem.attrib.get('resource-id', ''),
                class_name=elem.attrib.get('class', ''),
                package=elem.attrib.get('package', ''),
                content_desc=elem.attrib.get('content-desc', ''),
                clickable=elem.attrib.get('clickable', 'false').lower() == 'true',
                selected=elem.attrib.get('selected', 'false').lower() == 'true',
                checked=elem.attrib.get('checked', 'false').lower() == 'true',
                bounds=bounds,
                children=[]
            )
            for c_idx, child in enumerate(elem):
                child_node = parse_node(child, c_idx)
                if child_node:
                    node.children.append(child_node)
            return node

        try:
            root = ET.fromstring(xml_content)
            nodes = []
            for i, child in enumerate(root):
                parsed = parse_node(child, i)
                if parsed:
                    nodes.append(parsed)
            return nodes
        except Exception:
            return []

    async def _ui_nodes_or_empty(self, serial: str) -> List[UINode]:
        """For internal find/retry helpers: an unreadable screen counts as 'nothing found yet'."""
        try:
            return await self.dump_ui_hierarchy(serial)
        except UiDumpUnavailable:
            return []

    async def find_element(self, serial: str, text_query: str) -> Optional[Tuple[int, int, str]]:
        """Searches UI hierarchy with normalized alphanumeric matching to handle spacing and casing differences."""
        nodes = await self._ui_nodes_or_empty(serial)
        norm_query = re.sub(r'[^a-zA-Z0-9]', '', text_query).lower()
        if not norm_query:
            return None

        def search_nodes(n_list: List[UINode]) -> Optional[Tuple[int, int, str]]:
            for n in n_list:
                node_text = n.text or n.content_desc or ""
                norm_node = re.sub(r'[^a-zA-Z0-9]', '', node_text).lower()
                if norm_query in norm_node:
                    cx = (n.bounds[0] + n.bounds[2]) // 2
                    cy = (n.bounds[1] + n.bounds[3]) // 2
                    return cx, cy, node_text
                sub = search_nodes(n.children)
                if sub:
                    return sub
            return None

        return search_nodes(nodes)

    async def find_element_by_id(self, serial: str, resource_id: str) -> Optional[Tuple[int, int, UINode]]:
        """Searches UI hierarchy for matching resource-id and returns (center_x, center_y, node)."""
        nodes = await self._ui_nodes_or_empty(serial)

        def search_nodes(n_list: List[UINode]) -> Optional[Tuple[int, int, UINode]]:
            for n in n_list:
                if resource_id in n.resource_id:
                    cx = (n.bounds[0] + n.bounds[2]) // 2
                    cy = (n.bounds[1] + n.bounds[3]) // 2
                    return cx, cy, n
                sub = search_nodes(n.children)
                if sub:
                    return sub
            return None

        return search_nodes(nodes)

    async def find_element_by_desc(self, serial: str, content_desc: str) -> Optional[Tuple[int, int, UINode]]:
        """Searches UI hierarchy for matching content description."""
        nodes = await self._ui_nodes_or_empty(serial)
        target = content_desc.lower()

        def search_nodes(n_list: List[UINode]) -> Optional[Tuple[int, int, UINode]]:
            for n in n_list:
                if target in (n.content_desc or "").lower():
                    cx = (n.bounds[0] + n.bounds[2]) // 2
                    cy = (n.bounds[1] + n.bounds[3]) // 2
                    return cx, cy, n
                sub = search_nodes(n.children)
                if sub:
                    return sub
            return None

        return search_nodes(nodes)

    async def start_phone_call(self, serial: str, target: str) -> bool:
        """Initiates a phone call or launches dialer with the target number/contact."""
        # Clean digits if numeric
        digits = re.sub(r'[^\d+]', '', target)
        if digits and len(digits) >= 3:
            # Try CALL intent first, fallback to DIAL intent
            cmd = f"am start -a android.intent.action.CALL -d tel:{digits} 2>/dev/null || am start -a android.intent.action.DIAL -d tel:{digits}"
            out = await self.run_device_shell(serial, cmd)
            return "Starting: Intent" in out
        else:
            # Contact name: launch dialer and search
            await self.run_device_shell(serial, "am start -a android.intent.action.DIAL")
            await asyncio.sleep(1.0)
            await self.inject_text(serial, target)
            return True

    async def list_files(self, serial: str, path: str = "/sdcard/Download") -> List[Dict[str, Any]]:
        out = await self.run_device_shell(serial, f"ls -la {shlex.quote(path)}")
        files = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("total"):
                continue
            parts = line.split()
            if len(parts) >= 8:
                is_dir = parts[0].startswith('d')
                size = parts[4]
                name = " ".join(parts[7:])
                if name not in ('.', '..'):
                    files.append({
                        "name": name,
                        "is_dir": is_dir,
                        "size": size,
                        "path": f"{path.rstrip('/')}/{name}"
                    })
        return files

    async def pull_file(self, serial: str, remote_path: str, local_path: Path) -> bool:
        code, _, _ = await self.run_command("-s", serial, "pull", remote_path, str(local_path))
        return code == 0

    async def push_file(self, serial: str, local_path: Path, remote_path: str) -> bool:
        code, _, _ = await self.run_command("-s", serial, "push", str(local_path), remote_path)
        return code == 0

    async def get_telephony_state(self, serial: str) -> Dict[str, Any]:
        """Queries telephony state (0=idle, 1=ringing, 2=offhook/active) and incoming caller number."""
        out = await self.run_device_shell(serial, "dumpsys telephony.registry | grep -E 'mCallState|mCallIncomingNumber'")
        state = 0
        incoming_number = ""
        for line in out.splitlines():
            line = line.strip()
            if "mCallState=" in line:
                try:
                    s = int(line.split("mCallState=")[1].split()[0])
                    if s > state:
                        state = s
                except Exception:
                    pass
            elif "mCallIncomingNumber=" in line:
                num = line.split("mCallIncomingNumber=")[1].strip()
                if num and not incoming_number:
                    incoming_number = num
        return {
            "state": state,
            "incoming_number": incoming_number
        }

    async def answer_incoming_call(self, serial: str) -> bool:
        """Answers an incoming ringing phone call and turns on speakerphone."""
        # Keycode 5 (CALL) and 79 (HEADSETHOOK) are standard Android call answer key events
        await self.run_device_shell(serial, "input keyevent 5 || input keyevent 79")
        await asyncio.sleep(0.5)
        await self.enable_speakerphone(serial)
        return True

    async def end_call(self, serial: str) -> bool:
        """Ends an active or declines an incoming phone call."""
        # Keycode 6 (ENDCALL) and 79 (HEADSETHOOK)
        await self.run_device_shell(serial, "input keyevent 6 || input keyevent 79")
        return True

    async def enable_speakerphone(self, serial: str) -> bool:
        """Attempts to enable speakerphone during an active call."""
        try:
            res = await self.find_element(serial, "Speaker")
            if res:
                cx, cy, _ = res
                await self.inject_tap(serial, cx, cy)
                return True
        except Exception:
            pass
        return False

    async def _screen_asleep(self, serial: str) -> bool:
        out = await self.run_device_shell(serial, "dumpsys power | grep -E 'mWakefulness=|Display Power: state='")
        return any(k in out for k in ("mWakefulness=Asleep", "mWakefulness=Dozing", "state=OFF", "state=DOZE"))

    async def _wake_for_capture(self, serial: str):
        """Energise the camera rails if the phone is asleep — without unlocking it. Remembered so the screen
        is put back to sleep when the capture ends (it must not stay on showing a black curtain)."""
        if not hasattr(self, "_slept_before"):
            self._slept_before = set()
        if await self._screen_asleep(serial):
            self._slept_before.add(serial)
            await self.run_device_shell(serial, "input keyevent 224")
            await asyncio.sleep(0.3)

    async def _restore_sleep(self, serial: str):
        if serial in getattr(self, "_slept_before", set()):
            self._slept_before.discard(serial)
            await asyncio.sleep(0.8)   # let the capture screen close first
            await self.run_device_shell(serial, "input keyevent 223")   # back to sleep (still locked)

    async def capture_headless_photo_adb(self, serial: str, lens: str = "back", timeout: float = 12.0) -> Optional[Path]:
        try:
            return await self._capture_headless_photo_adb(serial, lens, timeout)
        finally:
            await self._restore_sleep(serial)

    async def _capture_headless_photo_adb(self, serial: str, lens: str = "back", timeout: float = 12.0) -> Optional[Path]:
        """Captures a photo headlessly directly from sensor via companion activity without launching camera app."""
        lens = "front" if lens == "front" else "back"  # never pass free text to the device shell
        import time
        # Ensure display and camera sensor rails are energized if screen is off, without unlocking keyguard
        await self._wake_for_capture(serial)

        existing = await self.run_device_shell(serial, "ls /sdcard/Download/capture_silent_*.jpg 2>/dev/null")
        existing_files = set(existing.split()) if existing else set()

        # Start HeadlessCameraActivity directly via ADB
        await self.run_device_shell(serial, f"am start -n com.operator.companion/.HeadlessCameraActivity --es action photo --es lens {lens}")

        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            await asyncio.sleep(0.5)
            latest = await self.run_device_shell(serial, "ls -t /sdcard/Download/capture_silent_*.jpg 2>/dev/null | head -n 1")
            latest_path = latest.strip()
            if latest_path and latest_path not in existing_files:
                dest_file = device_capture_dir(serial) / f"capture_silent_{int(time.time() * 1000)}_{serial[-4:]}.jpg"
                pull_ok = await self.pull_file(serial, latest_path, dest_file)
                if pull_ok and dest_file.exists():
                    # Immediately purge temporary photo from Android device so it is NOT saved on phone
                    await self.run_device_shell(serial, f"rm -f {shlex.quote(latest_path)}")
                    return dest_file
        return None

    async def record_headless_video_adb(self, serial: str, lens: str = "back", duration_sec: int = 5,
                                        timeout: Optional[float] = None,
                                        stop_event: Optional[asyncio.Event] = None) -> Optional[Path]:
        try:
            return await self._record_headless_video_adb(serial, lens, duration_sec, timeout, stop_event)
        finally:
            await self._restore_sleep(serial)

    async def _record_headless_video_adb(self, serial: str, lens: str = "back", duration_sec: int = 5,
                                         timeout: Optional[float] = None,
                                         stop_event: Optional[asyncio.Event] = None) -> Optional[Path]:
        """Records video (with sound) via the companion's headless camera screen, launched over ADB.
        duration_sec = 0 records until stop_event is set. Completion is signalled by the phone writing
        '<video>.done' next to the file, so a half-written recording is never copied."""
        lens = "front" if lens == "front" else "back"  # never pass free text to the device shell
        duration_sec = max(0, min(int(duration_sec), 3600))
        import time
        # Ensure display and camera sensor rails are energized if screen is off, without unlocking keyguard
        await self._wake_for_capture(serial)

        markers = "/sdcard/Download/video_silent_*.mp4.done"
        failed_marker = "/sdcard/Download/.acc_video_failed"
        existing = await self.run_device_shell(serial, f"ls {markers} 2>/dev/null; rm -f {failed_marker}")
        existing_done = set(existing.split()) if existing else set()

        await self.run_device_shell(serial, f"am start -n com.operator.companion/.HeadlessCameraActivity "
                                            f"--es action video --es lens {lens} --ei duration {duration_sec}")
        loop = asyncio.get_event_loop()
        started = loop.time()
        stop_sent_at = None
        retried = False
        finish_grace = max(timeout or 25.0, 25.0)
        while True:
            now = loop.time()
            if stop_event is not None and stop_event.is_set() and stop_sent_at is None:
                await self.run_device_shell(serial, "am broadcast -n com.operator.companion/.receiver.HeadlessCameraReceiver "
                                                    "-a com.operator.companion.ACTION_STOP_VIDEO")
                stop_sent_at = now
            if duration_sec and stop_sent_at is None and now - started > duration_sec + finish_grace:
                return None
            if stop_sent_at is not None and now - stop_sent_at > finish_grace + 30:
                return None
            if not duration_sec and now - started > 6 * 3600:
                return None
            listing = await self.run_device_shell(serial, f"ls {markers} {failed_marker} 2>/dev/null")
            names = set(listing.split()) if listing else set()
            if failed_marker in names:
                await self.run_device_shell(serial, f"rm -f {failed_marker}")
                if not retried and now - started < 6:
                    # Camera still closing after a previous capture: give it a moment and try once more
                    retried = True
                    await asyncio.sleep(1.5)
                    await self.run_device_shell(serial, f"am start -n com.operator.companion/.HeadlessCameraActivity "
                                                        f"--es action video --es lens {lens} --ei duration {duration_sec}")
                    started = loop.time()
                    continue
                return None
            new_done = sorted(names - existing_done - {failed_marker})
            if new_done:
                done = new_done[-1]
                video = done[: -len(".done")]
                dest_file = device_capture_dir(serial) / f"video_silent_{int(time.time() * 1000)}_{serial[-4:]}.mp4"
                # The marker lists every clip when the camera was switched mid-recording
                listed = [l.strip() for l in (await self.run_device_shell(serial, f"cat {shlex.quote(done)}")).splitlines()]
                listed = [l for l in listed if l.startswith("/") and l.endswith(".mp4")]
                if len(listed) > 1:
                    from .video_merge import merge_parts
                    local = []
                    for i, rp in enumerate(listed):
                        lp = DOWNLOADS_DIR / f".part_{int(time.time() * 1000)}_{i}.mp4"
                        if await self.pull_file(serial, rp, lp) and lp.exists():
                            local.append(lp)
                    await self.run_device_shell(serial, "rm -f " + " ".join(shlex.quote(x) for x in listed + [done]))
                    merged = await asyncio.get_event_loop().run_in_executor(None, merge_parts, local, dest_file)
                    return merged if merged and merged.exists() else None
                pull_ok = await self.pull_file(serial, video, dest_file)
                if pull_ok and dest_file.exists() and dest_file.stat().st_size > 0:
                    # Purge the temporary recording so it is NOT kept on the phone
                    await self.run_device_shell(serial, f"rm -f {shlex.quote(video)} {shlex.quote(done)}")
                    return dest_file
                dest_file.unlink(missing_ok=True)
                return None
            await asyncio.sleep(1.0)

