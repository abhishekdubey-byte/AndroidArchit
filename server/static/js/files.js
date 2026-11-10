// Device file manager — talks to /api/devices/{id}/fs/* (ADB-backed, all Android versions).
(() => {
  const $ = (id) => document.getElementById(id);
  const S = {
    devices: [], deviceId: null, online: true, path: null, parent: null, items: [],
    filter: "", showHidden: false, sort: { key: "name", dir: 1 },
    selected: new Set(), anchor: null, clipboard: null, listToken: 0,
  };
  const UPLOAD_CONCURRENCY = 2;
  const uploadQueue = [];
  let activeUploads = 0;

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };
  const base = () => `/api/devices/${encodeURIComponent(S.deviceId)}/fs`;

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---------- formatting ----------
  const fmtSize = (n) => {
    if (n < 1024) return `${n} B`;
    const u = ["KB", "MB", "GB", "TB"]; let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
  };
  const fmtDate = (s) => s ? new Date(s * 1000).toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  const ext = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");
  function iconFor(e) {
    if (e.is_dir) return "📁";
    if (e.is_link) return "🔗";
    const m = e.mime || "", x = ext(e.name);
    if (m.startsWith("image/")) return "🖼️";
    if (m.startsWith("video/")) return "🎬";
    if (m.startsWith("audio/") || ["opus", "amr", "m4a", "flac"].includes(x)) return "🎵";
    if (x === "apk" || x === "apks" || x === "xapk") return "📦";
    if (["zip", "rar", "7z", "tar", "gz"].includes(x)) return "🗜️";
    if (m === "application/pdf") return "📕";
    if (m.startsWith("text/") || ["json", "log", "md", "csv", "xml"].includes(x)) return "📝";
    return "📄";
  }
  const isMedia = (e) => !e.is_dir && /^(image|video)\//.test(e.mime || "");
  const isViewable = (e) => !e.is_dir && /^(image|video|audio)\//.test(e.mime || "");
  const thumbUrl = (e) => `${base()}/thumb?path=${encodeURIComponent(e.path)}&mtime=${e.mtime || 0}&size=256`;
  const inlineUrl = (e) => `${base()}/download?path=${encodeURIComponent(e.path)}&inline=true`;
  const transportLabel = (d) => d.transport === "adb" ? (d.serial.includes(":") ? "Wi-Fi ADB" : "USB") : "Remote";
  function iconEl(e, imgCls, iconCls = imgCls) {
    if (isMedia(e)) {
      const img = el("img", { class: imgCls, loading: "lazy", decoding: "async", alt: "", src: thumbUrl(e) });
      img.addEventListener("error", () => img.replaceWith(el("span", { class: iconCls, textContent: iconFor(e) })), { once: true });
      return img;
    }
    return el("span", { class: iconCls, textContent: iconFor(e) });
  }
  const previewable = (e) => /^(image|video|audio|text)\//.test(e.mime || "") || e.mime === "application/pdf" || ["json", "log", "md"].includes(ext(e.name));

  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k in n) n[k] = v;
      else n.setAttribute(k, v);
    }
    kids.flat().forEach((c) => c != null && n.append(c));
    return n;
  }

  function toast(msg, isErr = false) {
    const t = el("div", { class: `fm-toast${isErr ? " err" : ""}`, textContent: msg });
    $("fmToasts").append(t);
    setTimeout(() => t.remove(), isErr ? 6000 : 3000);
  }
  function banner(msg, retry) {
    const b = $("fmBanner");
    b.replaceChildren();
    if (!msg) { b.hidden = true; return; }
    b.append(el("span", { textContent: msg }));
    if (retry) b.append(el("button", { class: "fm-btn", textContent: "Retry", onclick: retry }));
    b.hidden = false;
  }

  // ---------- devices ----------
  async function loadDevices() {
    let list;
    try { list = await api("GET", "/api/devices"); } catch { return; }
    S.devices = list;
    const sel = $("fmDevice");
    const prev = sel.value;
    sel.replaceChildren(...list.map((d) => el("option", {
      value: d.id,
      textContent: `${d.name} · Android ${d.android_version} · ${d.status === "online" ? transportLabel(d) : "offline"}`,
    })));
    if (!S.deviceId && list.length) {
      const wanted = new URLSearchParams(location.search).get("device");
      const first = list.find((d) => d.id === wanted) || list.find((d) => d.status === "online") || list[0];
      await switchDevice(first.id, new URLSearchParams(location.search).get("path"));
    }
    if (S.deviceId) sel.value = S.deviceId; else if (prev) sel.value = prev;
    const cur = list.find((d) => d.id === S.deviceId);
    const nowOnline = !!cur && cur.status === "online";
    if (cur) $("fmDeviceInfo").textContent = cur.transport === "adb" ? `${transportLabel(cur)} · ${cur.serial}` : "Remote (companion relay)";
    if (S.deviceId && nowOnline !== S.online) {
      S.online = nowOnline;
      if (nowOnline) { banner(null); loadRoots(); navigate(S.path, false); }
      else banner("Device offline — reconnect USB, or check that the companion app on the phone has internet. This view resumes automatically.");
    }
    if (!list.length) banner("No Android devices detected. Connect a phone with USB debugging enabled.");
  }

  async function switchDevice(id, path) {
    S.deviceId = id;
    S.path = S.parent = null;
    S.items = [];
    S.selected.clear();
    S.online = true;
    banner(null);
    $("fmDevice").value = id;
    const dev = S.devices.find((d) => d.id === id);
    document.title = `Files · ${dev ? dev.name : id}`;
    updatePaste();
    loadRoots();
    await navigate(path || store.get(`fm.path.${id}`) || "/sdcard", false);
  }

  async function loadRoots() {
    const id = S.deviceId;
    try {
      const r = await api("GET", `${base()}/roots`);
      if (id !== S.deviceId) return;
      const mk = (x) => el("button", { class: "fm-side-item", textContent: x.label, title: x.path, onclick: () => navigate(x.path), "data-path": x.path });
      $("fmVolumes").replaceChildren(...r.volumes.map(mk));
      $("fmShortcuts").replaceChildren(...r.shortcuts.map(mk));
      const sp = r.internal_space;
      $("fmSpace").replaceChildren(...(sp ? [
        el("div", { textContent: `${fmtSize(sp.total - sp.free)} used of ${fmtSize(sp.total)}` }),
        el("div", { class: "fm-space-bar" }, el("div", { style: `width:${((1 - sp.free / sp.total) * 100).toFixed(1)}%` })),
      ] : []));
      highlightSide();
    } catch (e) { /* listing will surface the error */ }
  }
  function highlightSide() {
    document.querySelectorAll(".fm-side-item").forEach((b) => b.classList.toggle("active", b.dataset.path === S.path));
  }

  // ---------- listing ----------
  async function navigate(path, push = true) {
    if (!S.deviceId || !path) return;
    const token = ++S.listToken;
    $("fmStatus").textContent = "Loading…";
    try {
      const data = await api("GET", `${base()}/list?path=${encodeURIComponent(path)}`);
      if (token !== S.listToken) return;
      const changed = data.path !== S.path;
      S.path = data.path; S.parent = data.parent; S.items = data.items;
      if (changed) { S.selected.clear(); S.anchor = null; $("fmFilter").value = ""; S.filter = ""; }
      else S.selected = new Set([...S.selected].filter((p) => S.items.some((i) => i.path === p)));
      store.set(`fm.path.${S.deviceId}`, S.path);
      const url = `?device=${encodeURIComponent(S.deviceId)}&path=${encodeURIComponent(S.path)}`;
      if (push) history.pushState({ device: S.deviceId, path: S.path }, "", url);
      else history.replaceState({ device: S.deviceId, path: S.path }, "", url);
      banner(null);
      render();
    } catch (e) {
      if (token !== S.listToken) return;
      $("fmStatus").textContent = "";
      if (e.status === 409 && S.path !== path) return downloadPaths([path]); // symlink to a file
      if (!S.path) { S.path = path; render(); }
      if (e.status === 503) { S.online = false; banner(e.message); }
      else toast(e.message, true);
    }
  }
  const refresh = () => navigate(S.path, false);

  function visibleItems() {
    const f = S.filter.toLowerCase();
    const { key, dir } = S.sort;
    return S.items
      .filter((i) => (S.showHidden || !i.hidden) && (!f || i.name.toLowerCase().includes(f)))
      .sort((a, b) => (b.is_dir - a.is_dir) || (key === "name"
        ? dir * a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" })
        : dir * (a[key] - b[key]) || a.name.localeCompare(b.name)));
  }

  function render() {
    renderCrumbs();
    highlightSide();
    const items = visibleItems();
    const grid = currentView(items) === "grid";
    $("fmTable").hidden = grid;
    $("fmGrid").hidden = !grid;
    $("fmViewList").classList.toggle("active", !grid);
    $("fmViewGrid").classList.toggle("active", grid);
    if (grid) renderGrid(items); else renderRows(items);
    const empty = $("fmEmpty");
    empty.hidden = items.length > 0;
    empty.textContent = S.items.length ? "No items match the filter (or they are hidden)." : "This folder is empty.";
    const dirs = items.filter((i) => i.is_dir).length;
    const bytes = items.reduce((s, i) => s + (i.is_dir ? 0 : i.size), 0);
    $("fmStatus").textContent = `${dirs} folders, ${items.length - dirs} files · ${fmtSize(bytes)}` +
      (S.items.length !== items.length ? ` · ${S.items.length - items.length} hidden/filtered` : "");
    updateSelectionUi(items);
  }

  function renderGrid(items) {
    $("fmGrid").replaceChildren(...items.map((e) => {
      const card = el("div", { class: `fm-card${S.selected.has(e.path) ? " sel" : ""}`, title: e.name });
      const cb = el("input", { type: "checkbox", class: "fm-card-check", checked: S.selected.has(e.path),
        onclick: (ev) => { ev.stopPropagation(); toggleSel(e.path, ev.shiftKey, items); } });
      const thumb = el("div", { class: "fm-card-thumb" }, iconEl(e, "fm-card-img", "fm-card-icon"));
      if ((e.mime || "").startsWith("video/")) thumb.append(el("span", { class: "fm-play", textContent: "▶" }));
      card.append(cb, thumb, el("div", { class: "fm-card-name", textContent: e.name }),
        el("div", { class: "fm-card-meta", textContent: e.is_dir ? "Folder" : fmtSize(e.size) }));
      card.addEventListener("click", (ev) => (ev.ctrlKey || ev.metaKey || ev.shiftKey) ? toggleSel(e.path, ev.shiftKey, items) : open(e));
      return card;
    }));
  }

  function currentView(items) {
    const pref = store.get("fm.view");
    if (pref === "grid" || pref === "list") return pref;
    const files = items.filter((i) => !i.is_dir);
    const media = files.filter(isMedia).length;
    return media >= 4 && media >= files.length / 2 ? "grid" : "list";
  }

  function renderRows(items) {
    const rows = items.map((e) => {
      const tr = el("tr", { class: S.selected.has(e.path) ? "sel" : "" });
      tr.dataset.path = e.path;
      const cb = el("input", { type: "checkbox", checked: S.selected.has(e.path), onclick: (ev) => { ev.stopPropagation(); toggleSel(e.path, ev.shiftKey, items); } });
      const name = el("div", { class: `fm-name${e.hidden ? " fm-hidden-name" : ""}`, title: e.name, onclick: (ev) => { ev.stopPropagation(); open(e); } },
        iconEl(e, "fm-ico-thumb", "fm-ico"), el("span", { textContent: e.name }));
      const act = el("div", { class: "fm-row-act" },
        el("button", { textContent: "⬇", title: "Download", onclick: (ev) => { ev.stopPropagation(); downloadPaths([e.path]); } }),
        el("button", { textContent: "✎", title: "Rename", onclick: (ev) => { ev.stopPropagation(); renameItem(e); } }),
        el("button", { textContent: "🗑", title: "Delete", onclick: (ev) => { ev.stopPropagation(); deletePaths([e.path]); } }));
      tr.append(el("td", {}, cb), el("td", {}, name),
        el("td", { class: "fm-col-size", textContent: e.is_dir ? "" : fmtSize(e.size) }),
        el("td", { class: "fm-col-date", textContent: fmtDate(e.mtime) }), el("td", {}, act));
      tr.addEventListener("click", (ev) => toggleSel(e.path, ev.shiftKey, items, !(ev.ctrlKey || ev.metaKey)));
      tr.addEventListener("dblclick", () => open(e));
      return tr;
    });
    $("fmRows").replaceChildren(...rows);
  }

  function renderCrumbs() {
    const parts = S.path === "/" ? [] : S.path.split("/").slice(1);
    const crumbs = [el("button", { textContent: "/", onclick: (ev) => { ev.stopPropagation(); navigate("/"); } })];
    parts.forEach((p, i) => {
      const target = "/" + parts.slice(0, i + 1).join("/");
      if (i) crumbs.push(el("span", { textContent: "›" }));
      crumbs.push(el("button", { textContent: p, onclick: (ev) => { ev.stopPropagation(); navigate(target); } }));
    });
    $("fmCrumbs").replaceChildren(...crumbs);
    $("fmPathInput").value = S.path;
    $("fmUp").disabled = !S.parent;
  }

  // ---------- selection ----------
  function toggleSel(path, range, items, exclusive = false) {
    if (range && S.anchor) {
      const idx = items.map((i) => i.path);
      const [a, b] = [idx.indexOf(S.anchor), idx.indexOf(path)].sort((x, y) => x - y);
      if (a >= 0) idx.slice(a, b + 1).forEach((p) => S.selected.add(p));
    } else if (exclusive) {
      const only = S.selected.size === 1 && S.selected.has(path);
      S.selected.clear();
      if (!only) S.selected.add(path);
      S.anchor = path;
    } else {
      S.selected.has(path) ? S.selected.delete(path) : S.selected.add(path);
      S.anchor = path;
    }
    render();
  }
  function updateSelectionUi(items) {
    const n = S.selected.size;
    document.querySelectorAll("[data-sel]").forEach((b) => (b.disabled = n === 0));
    $("fmSelInfo").textContent = n ? `${n} selected` : "";
    const all = $("fmSelectAll");
    all.checked = n > 0 && items.length > 0 && items.every((i) => S.selected.has(i.path));
    all.indeterminate = n > 0 && !all.checked;
    const btn = $("fmSelAllBtn");
    btn.disabled = items.length === 0;
    btn.textContent = all.checked ? "✕ Clear selection" : "☑ Select all";
  }
  function updatePaste() {
    const c = S.clipboard;
    const btn = $("fmPaste");
    btn.hidden = !(c && c.deviceId === S.deviceId);
    if (c) btn.textContent = `Paste ${c.paths.length} (${c.mode})`;
  }

  // ---------- actions ----------
  function open(e) {
    if (e.is_dir || e.is_link) return navigate(e.path);
    if (isViewable(e)) return openViewer(e);
    if (previewable(e)) return window.open(`${base()}/download?path=${encodeURIComponent(e.path)}&inline=true`, "_blank", "noopener");
    downloadPaths([e.path]);
  }

  // ---------- in-page viewer (images / videos / audio, headless: nothing opens on the phone) ----------
  const V = { list: [], idx: -1 };
  function openViewer(e) {
    V.list = visibleItems().filter(isViewable);
    V.idx = Math.max(0, V.list.findIndex((i) => i.path === e.path));
    $("fmViewer").hidden = false;
    showViewer();
  }
  function closeViewer() {
    $("fmViewerStage").replaceChildren(); // stops video/audio
    $("fmViewer").hidden = true;
  }
  function stepViewer(d) {
    const n = V.idx + d;
    if (n >= 0 && n < V.list.length) { V.idx = n; showViewer(); }
  }
  function showViewer() {
    const e = V.list[V.idx];
    if (!e) return closeViewer();
    const m = e.mime || "";
    $("fmViewerName").textContent = e.name;
    $("fmViewerIdx").textContent = `${V.idx + 1} / ${V.list.length} · ${fmtSize(e.size)} · ${fmtDate(e.mtime)}`;
    $("fmViewerPrev").disabled = V.idx === 0;
    $("fmViewerNext").disabled = V.idx === V.list.length - 1;
    let node;
    if (m.startsWith("image/")) {
      // Thumbnail shows instantly; full resolution fades in when it arrives (important on slow remote links).
      const low = el("img", { class: "fm-viewer-media fm-low", alt: "", src: thumbUrl(e) });
      low.addEventListener("error", () => low.remove(), { once: true });
      const hi = el("img", { class: "fm-viewer-media fm-hi", alt: e.name });
      const note = el("div", { class: "fm-viewer-note", textContent: "Loading full resolution…" });
      hi.addEventListener("load", () => { hi.classList.add("ready"); low.remove(); note.remove(); }, { once: true });
      hi.addEventListener("error", () => { note.textContent = "This format can't be shown in the browser at full size; use Download."; }, { once: true });
      hi.src = inlineUrl(e);
      node = el("div", { class: "fm-viewer-imgwrap" }, low, hi, note);
    } else if (m.startsWith("video/")) {
      node = el("video", { class: "fm-viewer-media", controls: true, autoplay: true, playsInline: true, preload: "metadata", poster: thumbUrl(e), src: inlineUrl(e) });
    } else {
      node = el("audio", { class: "fm-viewer-audio", controls: true, autoplay: true, src: inlineUrl(e) });
    }
    $("fmViewerStage").replaceChildren(node);
    const next = V.list[V.idx + 1];
    if (next && isMedia(next)) new Image().src = thumbUrl(next);
  }

  function downloadPaths(paths) {
    const qs = paths.map((p) => `path=${encodeURIComponent(p)}`).join("&");
    const zipping = paths.length > 1 || S.items.some((i) => i.path === paths[0] && i.is_dir);
    // Hidden iframe: success triggers a browser download, an error page fires `load` so we can report it.
    const frame = el("iframe", { hidden: true });
    frame.addEventListener("load", () => {
      let msg = "Download failed";
      try { msg = JSON.parse(frame.contentDocument.body.textContent).detail || msg; } catch { /* not JSON */ }
      toast(msg, true);
      frame.remove();
    });
    frame.src = `${base()}/download?${qs}`;
    document.body.append(frame);
    setTimeout(() => frame.remove(), 30 * 60 * 1000);
    toast(zipping ? "Preparing zip on the laptop… the download starts when it is ready" : "Download started");
  }

  async function mkdir() {
    const name = prompt("New folder name:");
    if (!name) return;
    try { await api("POST", `${base()}/mkdir`, { parent: S.path, name }); toast(`Created ${name}`); refresh(); }
    catch (e) { toast(e.message, true); }
  }

  async function renameItem(e) {
    const name = prompt("Rename to:", e.name);
    if (!name || name === e.name) return;
    try { await api("POST", `${base()}/rename`, { path: e.path, new_name: name }); toast("Renamed"); refresh(); }
    catch (err) { toast(err.message, true); }
  }

  async function deletePaths(paths) {
    if (!paths.length) return;
    const label = paths.length === 1 ? `"${paths[0].split("/").pop()}"` : `${paths.length} items`;
    if (!confirm(`Permanently delete ${label} from the device? This cannot be undone.`)) return;
    try {
      await api("POST", `${base()}/delete`, { paths });
      paths.forEach((p) => S.selected.delete(p));
      toast(`Deleted ${label}`);
      refresh();
    } catch (e) { toast(e.message, true); refresh(); }
  }

  function setClipboard(mode) {
    S.clipboard = { deviceId: S.deviceId, paths: [...S.selected], mode };
    toast(`${S.clipboard.paths.length} item(s) ready to ${mode === "copy" ? "copy" : "move"} — open the destination and press Paste`);
    updatePaste();
  }

  async function paste() {
    const c = S.clipboard;
    if (!c || c.deviceId !== S.deviceId) return;
    $("fmStatus").textContent = `${c.mode === "copy" ? "Copying" : "Moving"} ${c.paths.length} item(s)…`;
    try {
      const { results } = await api("POST", `${base()}/transfer`, { paths: c.paths, dest_dir: S.path, mode: c.mode });
      const failed = results.filter((r) => !r.ok);
      failed.forEach((r) => toast(`${r.path.split("/").pop()}: ${r.error}`, true));
      if (results.length > failed.length) toast(`${c.mode === "copy" ? "Copied" : "Moved"} ${results.length - failed.length} item(s)`);
      if (c.mode === "move") S.clipboard = null;
      updatePaste();
      refresh();
    } catch (e) { toast(e.message, true); refresh(); }
  }

  // ---------- uploads ----------
  function enqueueUploads(entries) {
    if (!entries.length) return;
    const deviceId = S.deviceId, dir = S.path;
    $("fmTransfers").hidden = false;
    for (const { file, relpath } of entries) {
      const bar = el("div");
      const state = el("span", { textContent: "queued" });
      const row = el("div", { class: "fm-tr" },
        el("div", { class: "fm-tr-name" }, el("span", { textContent: relpath || file.name, title: `${dir}/${relpath || file.name}` }), state),
        el("div", { class: "fm-tr-bar" }, bar));
      $("fmTransfersList").prepend(row);
      uploadQueue.push({ file, relpath, deviceId, dir, row, bar, state });
    }
    pumpUploads();
  }

  function pumpUploads() {
    $("fmTransfersTitle").textContent = uploadQueue.length + activeUploads
      ? `Uploading · ${activeUploads} active, ${uploadQueue.length} queued` : "Transfers";
    while (activeUploads < UPLOAD_CONCURRENCY && uploadQueue.length) {
      const job = uploadQueue.shift();
      activeUploads++;
      uploadOne(job).finally(() => {
        activeUploads--;
        if (!activeUploads && !uploadQueue.length && job.deviceId === S.deviceId && job.dir === S.path) refresh();
        pumpUploads();
      });
    }
  }

  function uploadOne(job) {
    return new Promise((resolve) => {
      const fd = new FormData();
      fd.append("dir", job.dir);
      if (job.relpath) fd.append("relpath", job.relpath);
      fd.append("file", job.file, job.file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/devices/${encodeURIComponent(job.deviceId)}/fs/upload`);
      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        const pct = (ev.loaded / ev.total) * 100;
        job.bar.style.width = `${pct * 0.9}%`; // last 10% = adb push to the phone
        job.state.textContent = pct >= 100 ? "writing to phone…" : `${pct.toFixed(0)}%`;
      };
      const fail = (msg) => { job.row.classList.add("err"); job.bar.style.width = "100%"; job.state.textContent = "failed"; job.state.title = msg; toast(`${job.file.name}: ${msg}`, true); resolve(); };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          job.row.classList.add("done"); job.bar.style.width = "100%";
          job.state.textContent = `✓ ${fmtSize(job.file.size)}`;
          job.state.title = data.path || "";
          resolve();
        } else fail(typeof data.detail === "string" ? data.detail : `HTTP ${xhr.status}`);
      };
      xhr.onerror = () => fail("network error");
      job.state.textContent = "0%";
      xhr.send(fd);
    });
  }

  async function filesFromDrop(dt) {
    const out = [];
    const entries = [...dt.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (!entries.length) return [...dt.files].map((file) => ({ file, relpath: null }));
    const readAll = (reader) => new Promise((res, rej) => {
      const acc = [];
      const step = () => reader.readEntries((batch) => (batch.length ? (acc.push(...batch), step()) : res(acc)), rej);
      step();
    });
    async function walk(entry, prefix) {
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej));
        out.push({ file, relpath: prefix ? `${prefix}/${file.name}` : null });
      } else if (entry.isDirectory) {
        for (const child of await readAll(entry.createReader())) await walk(child, prefix ? `${prefix}/${entry.name}` : entry.name);
      }
    }
    for (const e of entries) await walk(e, "");
    return out;
  }

  // ---------- wiring ----------
  function bind() {
    $("fmDevice").addEventListener("change", (e) => switchDevice(e.target.value));
    $("fmNewTab").onclick = () => window.open(`/files.html?device=${encodeURIComponent(S.deviceId || "")}`, "_blank");
    $("fmUp").onclick = () => S.parent && navigate(S.parent);
    $("fmRefresh").onclick = refresh;
    $("fmMkdir").onclick = mkdir;
    $("fmDelete").onclick = () => deletePaths([...S.selected]);
    $("fmDownloadSel").onclick = () => downloadPaths([...S.selected]);
    $("fmBackupSel").onclick = async () => {
      const paths = [...S.selected];
      try {
        await api("POST", `/api/backup/${encodeURIComponent(S.deviceId)}/custom`, { paths });
        toast(`Added ${paths.length} item${paths.length > 1 ? "s" : ""} to this phone's backup — press Sync now on the Backup page`);
      } catch (e) { toast(e.message, true); }
    };
    $("fmCopy").onclick = () => setClipboard("copy");
    $("fmCut").onclick = () => setClipboard("move");
    $("fmPaste").onclick = paste;
    $("fmTransfersClear").onclick = () => {
      $("fmTransfersList").querySelectorAll(".done, .err").forEach((r) => r.remove());
      if (!activeUploads && !uploadQueue.length) $("fmTransfers").hidden = true;
    };
    $("fmViewList").onclick = () => { store.set("fm.view", "list"); render(); };
    $("fmViewGrid").onclick = () => { store.set("fm.view", "grid"); render(); };
    $("fmViewerClose").onclick = closeViewer;
    $("fmViewerPrev").onclick = () => stepViewer(-1);
    $("fmViewerNext").onclick = () => stepViewer(1);
    $("fmViewerDl").onclick = () => V.list[V.idx] && downloadPaths([V.list[V.idx].path]);
    $("fmViewerStage").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeViewer(); });
    $("fmUploadBtn").onclick = () => $("fmFileInput").click();
    $("fmUploadDirBtn").onclick = () => $("fmDirInput").click();
    $("fmFileInput").onchange = (e) => { enqueueUploads([...e.target.files].map((file) => ({ file, relpath: null }))); e.target.value = ""; };
    $("fmDirInput").onchange = (e) => { enqueueUploads([...e.target.files].map((file) => ({ file, relpath: file.webkitRelativePath || null }))); e.target.value = ""; };
    $("fmFilter").addEventListener("input", (e) => { S.filter = e.target.value; render(); });
    $("fmHidden").checked = S.showHidden = store.get("fm.hidden") === "1";
    $("fmHidden").addEventListener("change", (e) => { S.showHidden = e.target.checked; store.set("fm.hidden", S.showHidden ? "1" : "0"); render(); });
    $("fmSelAllBtn").onclick = () => {
      const items = visibleItems();
      const allOn = items.length > 0 && items.every((i) => S.selected.has(i.path));
      if (allOn) S.selected.clear(); else items.forEach((i) => S.selected.add(i.path));
      render();
    };
    $("fmSelectAll").addEventListener("click", (e) => {
      const items = visibleItems();
      if (e.target.checked) items.forEach((i) => S.selected.add(i.path)); else S.selected.clear();
      render();
    });
    document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.sort;
      S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : (k === "name" ? 1 : -1) };
      render();
    }));

    const toolbar = $("fmCrumbs").parentElement;
    $("fmCrumbs").addEventListener("click", () => { toolbar.classList.add("fm-editing"); $("fmPathInput").focus(); $("fmPathInput").select(); });
    $("fmPathInput").addEventListener("blur", () => toolbar.classList.remove("fm-editing"));
    $("fmPathInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const v = e.target.value.trim(); toolbar.classList.remove("fm-editing"); if (v) navigate(v.startsWith("/") ? v : `/${v}`); }
      if (e.key === "Escape") toolbar.classList.remove("fm-editing");
    });

    const drop = $("fmDrop");
    let depth = 0;
    drop.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); depth++; drop.classList.add("fm-dragging"); } });
    drop.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); });
    drop.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; drop.classList.remove("fm-dragging"); } });
    drop.addEventListener("drop", async (e) => {
      e.preventDefault(); depth = 0; drop.classList.remove("fm-dragging");
      try { enqueueUploads(await filesFromDrop(e.dataTransfer)); } catch (err) { toast(`Could not read dropped items: ${err.message || err}`, true); }
    });

    document.addEventListener("keydown", (e) => {
      if (!$("fmViewer").hidden) {
        if (e.key === "Escape") closeViewer();
        else if (e.key === "ArrowLeft") stepViewer(-1);
        else if (e.key === "ArrowRight") stepViewer(1);
        return;
      }
      if (e.target.matches("input, select, textarea")) return;
      if (e.key === "Backspace" && S.parent) { e.preventDefault(); navigate(S.parent); }
      else if (e.key === "Delete" && S.selected.size) deletePaths([...S.selected]);
      else if (e.key === "F5") { e.preventDefault(); refresh(); }
      else if (e.key === "Escape") { S.selected.clear(); render(); }
      else if ((e.ctrlKey || e.metaKey) && e.key === "a") { e.preventDefault(); visibleItems().forEach((i) => S.selected.add(i.path)); render(); }
      else if ((e.ctrlKey || e.metaKey) && e.key === "c" && S.selected.size) setClipboard("copy");
      else if ((e.ctrlKey || e.metaKey) && e.key === "x" && S.selected.size) setClipboard("move");
      else if ((e.ctrlKey || e.metaKey) && e.key === "v") paste();
    });
    window.addEventListener("popstate", (e) => {
      const st = e.state || {};
      if (st.device && st.device !== S.deviceId) switchDevice(st.device, st.path);
      else if (st.path) navigate(st.path, false);
    });
    window.addEventListener("beforeunload", (e) => { if (activeUploads || uploadQueue.length) e.preventDefault(); });
  }

  bind();
  loadDevices();
  setInterval(loadDevices, 4000);
})();
