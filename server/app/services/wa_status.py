"""WhatsApp: open Updates, watch every unread status and (optionally) heart-react to each one.

Works on any phone the control layer can drive (USB/ADB or the companion app remotely). It reads the real
screen each step (UI dump) instead of guessing coordinates:

  Updates list  → rows "Name, N unread status(es)" (com.whatsapp:id/status_row_container)
  Status viewer → header name + "Status i of n" (playback_progress) + heart (status_like_button)

WhatsApp moves on to the next contact's unread status by itself; we tap the right side to advance, and after the
viewer closes we re-check the list and continue until no unread status is left. Each status is liked at most
once per run (a second tap would remove the like), and one that is already liked is left alone.
"""
import asyncio
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional

WA = "com.whatsapp"
ANY_ROW = re.compile(r"^(?P<name>.+?),\s*(?P<n>\d+)\s+unread status", re.I)
UNREAD = re.compile(r"^(?P<name>.+?),\s*(?P<n>[1-9]\d*)\s+unread status", re.I)   # "0 unread" = all seen
PROGRESS = re.compile(r"Status\s+(\d+)\s+of\s+(\d+)", re.I)
# WhatsApp shows sponsored ads between statuses: skip them (never like, never count as a contact's status)
AD_LABEL = re.compile(r"^\s*(sponsored|ad|promoted|advertisement)\s*[·•]?\s*$", re.I)
AD_CTA = re.compile(r"^\s*(learn more|shop now|install( now)?|sign up|book now|order now|get offer|send message|"
                    r"visit (the )?website|download|buy now|apply now|watch more|contact us)\s*$", re.I)


def _is_ad(nodes) -> bool:
    for n in nodes:
        for t in (n.text or "", n.content_desc or ""):
            if AD_LABEL.match(t) or "sponsored" in t.lower():
                return True
    # an ad usually has no heart button but a call-to-action instead
    has_heart = any(_rid(n) in ("status_like_button", "status_like_button_container") for n in nodes)
    return not has_heart and any(AD_CTA.match(n.text or "") for n in nodes)


def _flat(nodes) -> List:
    out = []
    stack = list(nodes or [])
    while stack:
        n = stack.pop(0)
        out.append(n)
        stack[0:0] = list(n.children or [])
    return out


def _rid(n) -> str:
    return (n.resource_id or "").split(":id/")[-1]


def _center(n):
    x1, y1, x2, y2 = n.bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


async def run(ctl, serial: str, like: bool = True, progress: Optional[Callable[[str], Awaitable[None]]] = None,
              include_seen: bool = False,
              max_statuses: int = 400, max_minutes: float = 25.0, dwell: float = 0.0) -> Dict:
    """Returns {"viewed": int, "liked": int, "contacts": [names], "stopped": reason}."""
    async def say(msg: str):
        if progress:
            await progress(msg)

    deadline = time.time() + max_minutes * 60
    viewed, liked, ads, contacts = 0, 0, 0, []
    seen = set()          # (name, i, n) already handled in this run
    misses = 0            # consecutive dumps that showed neither viewer nor list
    heart_xy = None       # ❤ position, remembered after the first find (it doesn't move)
    prev_key = None
    back_steps = 0        # taps back after an accidental skip (bounded)

    async def dump():
        return _flat(await ctl._ui_nodes_or_empty(serial))  # unreadable screen = a miss, retried below

    async def open_updates() -> List:
        nodes = await dump()
        for _ in range(3):                        # a status viewer left open: close it first
            if not any(_rid(n) == "playback_progress" for n in nodes):
                break
            await ctl.inject_key(serial, "BACK")
            await asyncio.sleep(1.2)
            nodes = await dump()
        if not any(_rid(n) in ("status_row_container", "my_status_row_container") for n in nodes):
            tab = next((n for n in nodes if (n.content_desc or "").strip().lower().startswith("updates")), None)
            if tab is None:
                tab = next((n for n in nodes if (n.text or "").strip().lower() == "updates"), None)
            if tab is not None and tab.bounds[2] > tab.bounds[0]:
                await ctl.inject_tap(serial, *_center(tab))
                await asyncio.sleep(2.0)
                nodes = await dump()
        return nodes

    def unread_rows(nodes):
        # include_seen (repair mode): every contact not visited yet in this run, read or not
        pat = ANY_ROW if include_seen else UNREAD
        rows = []
        for n in nodes:
            if _rid(n) == "status_row_container":
                m = pat.match(n.content_desc or "")
                if m and not (include_seen and m.group("name") in contacts):
                    rows.append((m.group("name"), int(m.group("n")), n))
        return rows

    async def scroll_list(down: bool):
        # swipe inside the Updates list (middle of the screen)
        if down:
            await ctl.inject_swipe(serial, 540, 1700, 540, 700, 350)
        else:
            await ctl.inject_swipe(serial, 540, 700, 540, 1700, 250)
        await asyncio.sleep(0.9)

    async def find_unread(collect: bool = False):
        """Scroll the whole Updates list (it's longer than one screen). Returns (all unread names, a visible
        unread row to tap or None). Leaves the list at the position where that row is on screen."""
        names, last_sig = [], None
        for _ in range(25):                      # up to ~25 screens of contacts
            nodes = await dump()
            rows = unread_rows(nodes)
            for r in rows:
                if r[0] not in names:
                    names.append(r[0])
            if rows and not collect:
                return names, rows[0][2]
            sig = tuple(n.content_desc for n in nodes if _rid(n) == "status_row_container")
            if sig == last_sig:                  # reached the end of the list
                break
            last_sig = sig
            await scroll_list(True)
        if collect:
            for _ in range(25):                  # back to the top, then hand back the first unread row
                nodes = await dump()
                if any(_rid(n) == "my_status_row_container" for n in nodes):
                    break
                await scroll_list(False)
            nodes = await dump()
            rows = unread_rows(nodes)
            if not rows and names:
                return names, (await find_unread())[1]
            return names, rows[0][2] if rows else None
        return names, None

    await ctl.launch_package(serial, WA)
    await asyncio.sleep(3.0)
    nodes = await open_updates()
    if not any(_rid(n) in ("status_row_container", "my_status_row_container") for n in nodes):
        return {"viewed": 0, "liked": 0, "contacts": [], "stopped": "could not open WhatsApp's Updates tab (is the phone unlocked?)"}
    await say("Scanning the whole Updates list for unread statuses…")
    names, first = await find_unread(collect=True)
    if not first:
        return {"viewed": 0, "liked": 0, "contacts": [], "stopped": "no unread statuses"}
    await say(f"{len(names)} contact(s) with unread statuses: " + ", ".join(names[:6]) + ("…" if len(names) > 6 else ""))
    await ctl.inject_tap(serial, *_center(first))
    await asyncio.sleep(1.4)

    stopped = "all statuses viewed"
    while True:
        if time.time() > deadline:
            stopped = "time limit reached"
            break
        if viewed >= max_statuses:
            stopped = "safety limit reached"
            break
        nodes = await dump()
        prog = next((n for n in nodes if _rid(n) == "playback_progress"), None)
        if prog is None:
            # Viewer closed: back on the list? continue with anything still unread — anywhere in the list
            if any(_rid(n) in ("status_row_container", "my_status_row_container") for n in nodes):
                misses = 0
                _, row = await find_unread()
                if row is not None:
                    await ctl.inject_tap(serial, *_center(row))
                    await asyncio.sleep(1.4)
                    continue
                break     # scrolled the whole list: nothing unread left
            misses += 1
            if misses >= 3:
                stopped = "WhatsApp left the status screen"
                break
            await asyncio.sleep(1.0)
            continue
        misses = 0
        m = PROGRESS.search(prog.content_desc or prog.text or "")
        name_node = next((n for n in nodes if _rid(n) == "name"), None)
        name = (name_node.text if name_node else "") or "?"
        i, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        key = (name, i, total)
        container = next((n for n in nodes if _rid(n) == "status_container"), None)
        if _is_ad(nodes):
            if ("ad", key) not in seen:
                seen.add(("ad", key))
                ads += 1
                await say(f"Skipped a sponsored ad · viewed {viewed}" + (f", liked {liked}" if like else ""))
            x1, y1, x2, y2 = container.bounds if container is not None else (0, 0, 1080, 2148)
            await ctl.inject_tap(serial, x1 + int((x2 - x1) * 0.88), y1 + int((y2 - y1) * 0.45))
            await asyncio.sleep(1.0)
            continue
        # A short status advanced by itself while we were reading → we jumped one: go back to it (left edge)
        if (prev_key and container is not None and key[0] == prev_key[0] and key[2] == prev_key[2]
                and key[1] > prev_key[1] + 1 and (key[0], key[1] - 1, key[2]) not in seen and back_steps < 50):
            back_steps += 1
            x1, y1, x2, y2 = container.bounds
            await ctl.inject_tap(serial, x1 + int((x2 - x1) * 0.10), y1 + int((y2 - y1) * 0.45))
            await asyncio.sleep(0.4)
            continue
        prev_key = key
        if key not in seen:
            seen.add(key)
            viewed += 1
            if name not in contacts:
                contacts.append(name)
            await asyncio.sleep(dwell)            # let it register as viewed
            if like:
                heart = next((n for n in nodes if _rid(n) == "status_like_button"), None)
                box = next((n for n in nodes if _rid(n) == "status_like_button_container"), None)
                # liked state = the "selected" flag (the label is "Heart reaction" either way). Without that flag
                # (old companion app) we can't tell, so we never tap — a tap on a liked heart removes the like.
                flags = [x.selected for x in (heart, box) if x is not None]
                already = any(f is True for f in flags)
                unknown = not flags or all(f is None for f in flags)
                if unknown:
                    already = True            # can't see the liked state → leave it, never risk un-liking
                target = box or heart
                if target is not None and target.bounds[3] > target.bounds[1]:
                    heart_xy = _center(target)
                if (target is not None or heart_xy) and not already:
                    await ctl.inject_tap(serial, *(heart_xy or _center(target)))
                    liked += 1
                    await asyncio.sleep(0.25)
            await say(f"{name} · status {i}/{total} · viewed {viewed}" + (f", liked {liked}" if like else ""))
        if container is not None:
            x1, y1, x2, y2 = container.bounds
            await ctl.inject_tap(serial, x1 + int((x2 - x1) * 0.88), y1 + int((y2 - y1) * 0.45))
        else:
            await ctl.inject_tap(serial, 950, 1000)
        await asyncio.sleep(0.35)

    # leave WhatsApp on its Updates list (closes the viewer if still open)
    nodes = await dump()
    if any(_rid(n) == "playback_progress" for n in nodes):
        await ctl.inject_key(serial, "BACK")
    return {"viewed": viewed, "liked": liked, "ads": ads, "contacts": contacts, "stopped": stopped}
