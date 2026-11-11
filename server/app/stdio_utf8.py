"""UTF-8 console streams on every OS (no side effects on import)."""
import sys


def force_utf8_stdio(streams=None) -> None:
    """Make console streams UTF-8 so printing '→', '·' or emoji never crashes on a Windows (cp1252) console.
    Streams without reconfigure() (StringIO, test doubles, None) are left alone. Harmless on Linux/macOS."""
    if streams is None:
        streams = [sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__]
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # stream already closed or not reconfigurable: nothing to fix
