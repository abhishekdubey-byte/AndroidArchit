"""Live terminal view for the server.

In a terminal the server shows one full-screen panel that is redrawn in place (like `htop`):
header + relay status, a table with one row per phone, the latest events (identical repeats
collapse into a ×N counter) and request counters. Nothing scrolls; the complete history goes
to storage/logs/server.log. When output is redirected, plain aligned lines are written instead.
"""
import atexit
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from .stdio_utf8 import force_utf8_stdio

force_utf8_stdio()  # before anything below (or any service) prints: Windows consoles default to cp1252

TTY = sys.__stdout__.isatty()
COLOR = TTY and not os.getenv("NO_COLOR")
LOG_DIR = Path(__file__).resolve().parent.parent / "storage" / "logs"

# print() prefixes -> (label, colour)
TAGS = {
    "WebSocketHub": ("PHONE", "35"), "CloudBridge": ("RELAY", "34"), "DeviceManager": ("DEVICE", "36"),
    "Startup": ("SERVER", "32"), "Shutdown": ("SERVER", "33"), "Remote": ("REMOTE", "34"),
    "AIAgent": ("AI", "95"), "AppLifecycle": ("APP", "32"), "Diagnostics": ("DIAG", "33"),
}
WARN_WORDS = re.compile(r"\b(lost|refused|rejected|error|failed|offline|timed out|retrying|paused)\b", re.I)
NOISE = {"connection open", "connection closed", "Waiting for application startup.",
         "Waiting for application shutdown.", "Application shutdown complete."}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


def _visible_len(s: str) -> int:
    return len(ANSI.sub("", s))


def _fit(s: str, width: int) -> str:
    """Truncate a (possibly coloured) string to `width` visible characters."""
    if _visible_len(s) <= width:
        return s
    out, n = [], 0
    for tok in re.split(r"(\x1b\[[0-9;]*[A-Za-z])", s):
        if tok.startswith("\x1b"):
            out.append(tok)
            continue
        room = width - 1 - n
        if room <= 0:
            break
        out.append(tok[:room])
        n += len(tok[:room])
    return "".join(out) + "…" + ("\x1b[0m" if COLOR else "")


def status_color(code: int) -> str:
    return "32" if code < 300 else "36" if code < 400 else "33" if code < 500 else "31"


class Event:
    __slots__ = ("ts", "label", "color", "level", "msg", "count")

    def __init__(self, label, color, level, msg):
        self.ts, self.label, self.color, self.level, self.msg, self.count = time.time(), label, color, level, msg, 1

    def render(self, with_count=True) -> str:
        mark = {"ERROR": _c("1;31", "✖"), "WARNING": _c("1;33", "▲")}.get(self.level, _c(self.color, "●"))
        count = f"  {_c('1;33', f'×{self.count}')}" if with_count and self.count > 1 else ""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"{_c('2', stamp)}  {mark} {_c('1;' + self.color, f'{self.label:<7}')} {self.msg}{count}"


class Console:
    def __init__(self):
        self.out = sys.__stdout__
        self.lock = threading.RLock()      # protects the data; never held while writing to the terminal
        self.wlock = threading.Lock()      # serialises terminal writes (a stalled terminal blocks only the painter)
        self.closed = False
        self.file = None
        self.events: deque = deque(maxlen=200)
        self.requests: "OrderedDict[str, int]" = OrderedDict()
        self.req_errors = 0
        self.last_request = 0.0
        self.provider = None
        self.started = time.time()
        self.panel_on = False
        self.dirty = True
        self._last_plain_status = 0.0

    def start(self):
        """Called once by the real server process (not uvicorn's reloader parent)."""
        if self.file is not None:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = logging.getLogger("acc.file")
        log.propagate = False
        h = logging.handlers.RotatingFileHandler(LOG_DIR / "server.log", maxBytes=5_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        h.handleError = lambda record: None  # e.g. disk full: never recurse back into the console
        log.addHandler(h)
        log.setLevel(logging.INFO)
        with self.lock:
            for ev in self.events:  # events logged before start()
                log.info(f"{ev.level:<7} {ev.label:<7} {ANSI.sub('', ev.msg)}")
        self.file = log
        if TTY:
            threading.Thread(target=self.loop, name="acc-console", daemon=True).start()

    # ---------- events ----------
    def event(self, label: str, color: str, msg: str, level: str = "INFO"):
        if self.file is not None:
            self.file.info(f"{level:<7} {label:<7} {ANSI.sub('', msg)}")
        with self.lock:
            for ev in list(self.events)[-5:]:
                if ev.label == label and ev.msg == msg:  # collapse repeats instead of adding lines
                    ev.count += 1
                    ev.ts = time.time()
                    self.events.remove(ev)
                    self.events.append(ev)
                    break
            else:
                ev = Event(label, color, level, msg)
                self.events.append(ev)
                if not TTY:
                    with self.wlock:
                        self.out.write(ANSI.sub("", ev.render()) + "\n")
                        self.out.flush()
            self.dirty = True

    def request(self, key: str, error: bool = False):
        with self.lock:
            self.requests[key] = self.requests.get(key, 0) + 1
            self.requests.move_to_end(key)
            while len(self.requests) > 8:
                self.requests.popitem(last=False)
            self.req_errors += error
            self.last_request = time.time()
            self.dirty = True
            if not TTY and time.monotonic() - self._last_plain_status > 60:
                self._last_plain_status = time.monotonic()
                self.out.write(ANSI.sub("", self._requests_line(200)) + "\n")
                self.out.flush()

    # ---------- panel ----------
    def _requests_line(self, width: int) -> str:
        if not self.requests:
            return _c("2", "HTTP   no requests yet")
        recent = list(self.requests.items())[::-1][:4]
        parts = "  ·  ".join(f"{k} ×{n}" for k, n in recent)
        err = _c("1;31", f"{self.req_errors} errors") if self.req_errors else _c("2", "0 errors")
        return _fit(f"{_c('1;36', 'HTTP')}   {_c('2', time.strftime('%H:%M:%S', time.localtime(self.last_request)))}  "
                    f"{parts}  ·  {err}", width)

    def render(self):
        cols, rows = shutil.get_terminal_size((120, 32))
        width = max(40, cols - 1)
        state = {}
        if self.provider:
            try:
                state = self.provider() or {}
            except Exception as e:
                state = {"error": str(e)}
        rule = _c("2", "─" * width)
        up = int(time.time() - self.started)
        lines = [
            _fit(f"{_c('1;35', '⚡ ANDROID CONTROL CENTRE')}   {_c('2', time.strftime('%H:%M:%S'))}  "
                 f"{_c('2', f'up {up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}')}"
                 f"   {_c('2', 'dashboards open: ' + str(state.get('dashboards', 0)))}", width),
            _fit(f"{_c('4', 'http://localhost:8000')}   Files /files.html   Pair phones /pair.html", width),
        ]
        relay = state.get("relay") or {}
        if relay.get("url"):
            st = relay.get("state")
            badge = (_c("1;32", "● connected") if st == "connected" else _c("1;33", "● connecting")
                     if st == "connecting" else _c("1;31", "● " + (st or "off")))
            host = re.sub(r"^wss?://", "", relay["url"])
            extra = f" · {relay.get('phones', 0)} phone(s) via relay" if st == "connected" else f" · {relay.get('error') or ''}"
            lines.append(_fit(f"Relay  {badge}  {host}{_c('2', extra)}", width))
        else:
            lines.append(_c("2", "Relay  not configured (same Wi-Fi / USB only) — set it up on /pair.html"))
        lines.append(rule)

        devices = state.get("devices") or []
        head = f"{'PHONE':<28} {'ANDROID':<8} {'LINK':<16} {'BATTERY':<8} STATUS"
        lines.append(_c("1", _fit(head, width)))
        if not devices:
            lines.append(_c("2", "  no phones yet — plug one in over USB or pair it on /pair.html"))
        for d in devices:
            status = d.get("status", "")
            st = _c("32", status) if status == "online" else _c("31", status) if status == "offline" else _c("33", status)
            link = d.get("link", "—")
            link_c = _c("2", f"{link:<16}") if link == "—" else f"{link:<16}"
            lines.append(_fit(f"{d.get('name', '')[:28]:<28} {str(d.get('android', '')):<8} {link_c} "
                              f"{str(d.get('battery', '')):<8} {st}", width))
        lines.append(rule)

        footer = [rule, self._requests_line(width),
                  _c("2", _fit(f"Full log: {LOG_DIR / 'server.log'}   ·   Ctrl+C to stop", width))]
        room = max(3, rows - len(lines) - len(footer) - 1)
        lines.append(_c("1", "RECENT EVENTS"))
        recent = list(self.events)[-(room - 1):]
        lines += [_fit(ev.render(), width) for ev in recent]
        lines += [""] * max(0, room - 1 - len(recent))
        lines += footer
        return "\n".join("\x1b[2K" + ln for ln in lines[:rows - 1])

    def draw(self):
        if not TTY or self.closed:
            return
        with self.lock:
            frame = self.render()
            self.dirty = False
        with self.wlock:
            if self.closed:
                return
            if not self.panel_on:
                self.out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
                self.panel_on = True
            self.out.write("\x1b[H" + frame + "\x1b[J")
            self.out.flush()

    def loop(self):
        last = 0.0
        while True:
            time.sleep(0.25)
            if self.dirty or time.time() - last >= 1.0:  # events immediately, clock once a second
                try:
                    self.draw()
                except Exception:
                    pass
                last = time.time()

    def close(self):
        self.closed = True
        with self.lock:
            tail = list(self.events)[-15:]
        with self.wlock:
            if self.panel_on:
                self.out.write("\x1b[?25h\x1b[?1049l")  # restore normal screen + cursor
                self.panel_on = False
                if tail:
                    self.out.write("Last events (full log: %s):\n" % (LOG_DIR / "server.log"))
                    for ev in tail:
                        self.out.write(ev.render() + "\n")
                self.out.flush()


console = Console()
atexit.register(console.close)


def set_panel_provider(fn):
    """fn() -> {"devices": [...], "relay": {...}, "dashboards": int}; must be fast and do no I/O.
    Also starts the live view + log file (only the real server process calls this)."""
    console.provider = fn
    console.start()


def shutdown():
    console.close()


_reentry = threading.local()


class _LineWriter:
    """Routes print() output into the event list."""

    def __init__(self):
        self.buf = ""

    def write(self, s: str):
        if getattr(_reentry, "active", False):  # error while logging: bypass the console
            return sys.__stderr__.write(s)
        _reentry.active = True
        try:
            return self._write(s)
        finally:
            _reentry.active = False

    def _write(self, s: str):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            log_print(line)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return TTY

    def fileno(self):
        return sys.__stdout__.fileno()


def log_print(line: str):
    if not line.strip() or set(line.strip()) <= {"="}:
        return
    m = re.match(r"^\[([A-Za-z ]+)\]\s?(.*)$", line)
    if not m:
        console.event("LOG", "37", line.strip())
        return
    label, color = TAGS.get(m.group(1), (m.group(1).upper()[:7], "37"))
    msg = m.group(2)
    console.event(label, color, msg, "WARNING" if WARN_WORDS.search(msg) else "INFO")


if not isinstance(sys.stdout, _LineWriter):
    sys.stdout = _LineWriter()
    sys.stderr = _LineWriter()


class ConsoleHandler(logging.Handler):
    """uvicorn's own messages (startup, WebSocket handshakes, errors)."""

    def emit(self, record):
        try:
            msg = record.getMessage()
            if msg in NOISE:
                return
            ws = re.match(r'^(.*?) - "WebSocket (\S+)" (.*)$', msg)
            if ws:
                client, path, result = ws.groups()
                ok = "accepted" in result
                text = f"{path}  {_c('32' if ok else '33', result.strip('[]'))}  {_c('2', '· ' + client.rsplit(':', 1)[0])}"
                console.event("WS", "36", text, "INFO" if ok else "WARNING")
                return
            if record.exc_info and self.formatter:
                msg += " | " + self.formatter.formatException(record.exc_info).replace("\n", " | ")
            console.event("SERVER", "32", msg, record.levelname)
        except Exception:
            self.handleError(record)


class AccessHandler(logging.Handler):
    """uvicorn.access: routine successful GETs only bump counters; anything unusual becomes an event."""

    def emit(self, record):
        try:
            client, method, path, _ver, status = record.args
            status = int(status)
        except Exception:
            console.event("HTTP", "36", record.getMessage())
            return
        if method in ("GET", "HEAD") and 200 <= status < 400:
            console.request(str(path).split("?")[0])
        else:
            level = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
            console.request(str(path).split("?")[0], error=status >= 500)
            console.event("HTTP", "36", f"{_c('1;' + status_color(status), str(status))}  {method} "
                                        f"{str(path).split('?')[0]}  {_c('2', '· ' + str(client).rsplit(':', 1)[0])}", level)


LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"default": {"format": "%(message)s"}},
    "handlers": {
        "default": {"()": "app.logsetup.ConsoleHandler", "formatter": "default"},
        "access": {"()": "app.logsetup.AccessHandler", "formatter": "default"},
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}
