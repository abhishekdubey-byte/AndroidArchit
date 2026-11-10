// Phone Backup page: one card per phone; selection saved on change; progress by polling /api/backup.
(() => {
  const $ = (id) => document.getElementById(id);
  const PRESETS = [["camera", "Camera"], ["screenshots", "Screenshots"], ["whatsapp", "WhatsApp media"],
                   ["documents", "Documents"], ["download", "Download"]];

  function el(tag, props = {}, ...kids) {
    const n = Object.assign(document.createElement(tag), props);
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }
  function toast(msg, isErr = false) {
    const t = el("div", { className: `fm-toast${isErr ? " err" : ""}`, textContent: msg });
    $("fmToasts").append(t);
    setTimeout(() => t.remove(), isErr ? 6000 : 3000);
  }
  async function api(method, url, body) {
    const res = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : undefined,
                                   body: body ? JSON.stringify(body) : undefined });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `HTTP ${res.status}`);
    return data;
  }
  const mb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.ceil(b / 1e3)} KB`);
  const ago = (t) => {
    const s = Math.round(Date.now() / 1000 - t);
    return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
  };

  const cards = new Map(); // phone_key -> {root, refs}
  let timer = null;

  function progressText(p) {
    if (!p) return "Starting…";
    if (p.phase === "scanning") return `Scanning ${p.current || ""}`;
    const pct = p.bytes_total ? Math.min(100, Math.round((100 * p.bytes_done) / p.bytes_total)) : 0;
    return `Copying ${p.files_done}/${p.files_total} files · ${mb(p.bytes_done)} of ${mb(p.bytes_total)} (${pct}%) · ${(p.current || "").split("/").pop()}`;
  }

  function lastRunText(r) {
    if (!r) return "Never backed up";
    const when = r.finished ? ` · ${ago(r.finished)}` : "";
    if (r.status === "done") return `Last sync: ${r.copied} new · ${mb(r.bytes || 0)} · ${r.kept_deleted} kept from phone deletions · ${r.errors.length} errors${when}`;
    return `Last sync ${r.status}: ${r.message}${when}`;
  }

  function build(ph) {
    const r = {};
    const key = ph.phone_key;
    const cur = () => cards.get(key).ph; // latest data: the device id changes when a phone comes online or switches USB/relay
    const root = el("section", { className: "bk-card" });
    r.badge = el("span", { className: "bk-badge" });
    root.append(el("div", { className: "bk-head" }, el("span", { className: "bk-name", textContent: ph.name, title: ph.phone_key }), r.badge));
    root.append(el("div", { className: "bk-folder", textContent: `💾 ${ph.folder}`, title: "Laptop folder for this phone" }));

    root.append(el("div", { className: "bk-label", textContent: "Back up" }));
    const presets = el("div", { className: "bk-presets" });
    r.checks = {};
    for (const [key, label] of PRESETS) {
      const cb = el("input", { type: "checkbox", onchange: () => save(cur()) });
      r.checks[key] = cb;
      presets.append(el("label", { className: "fm-check" }, cb, ` ${label}`));
    }
    root.append(presets);
    r.custom = el("div", { className: "bk-custom" });
    root.append(r.custom);
    r.input = el("input", { className: "fm-input", placeholder: "/sdcard/Some/Folder or file", spellcheck: false });
    const addBtn = el("button", { className: "fm-btn", textContent: "＋ Add", onclick: () => addCustom(cur()) });
    r.input.addEventListener("keydown", (e) => { if (e.key === "Enter") addCustom(cur()); });
    r.addRow = el("div", { className: "bk-add" }, r.input, addBtn);
    root.append(r.addRow);

    r.sync = el("button", { className: "fm-btn fm-primary", textContent: "⟳ Sync now", onclick: () => run(cur(), "run") });
    r.cancel = el("button", { className: "fm-btn fm-danger", textContent: "Cancel", onclick: () => run(cur(), "cancel") });
    root.append(el("div", { className: "bk-actions" }, r.sync, r.cancel));
    r.bar = el("div");
    r.barWrap = el("div", { className: "bk-bar" }, r.bar);
    r.status = el("div", { className: "bk-status" });
    r.errors = el("details", { className: "bk-errors" });
    root.append(r.barWrap, r.status, r.errors);
    return { root, r, ph };
  }

  function render(card, ph) {
    const { root, r } = card;
    card.ph = ph;
    const editable = !!ph.device_id;
    root.classList.toggle("offline", !ph.online);
    r.badge.textContent = ph.online ? (ph.transport === "usb" ? "🔌 USB" : "📡 Remote") : "Offline";
    r.badge.classList.toggle("on", ph.online);
    const sel = ph.selection || { presets: [], custom: [] };
    for (const [key] of PRESETS) {
      if (document.activeElement !== r.checks[key]) r.checks[key].checked = sel.presets.includes(key);
      r.checks[key].disabled = !editable;
    }
    r.custom.replaceChildren(...sel.custom.map((p) => el("div", { className: "bk-row" },
      el("span", { textContent: p, title: p }),
      editable ? el("button", { className: "fm-btn", textContent: "✕", title: "Remove", onclick: () => removeCustom(card.ph, p) }) : null)));
    r.addRow.hidden = !editable;
    r.sync.disabled = !ph.online || ph.running;
    r.cancel.hidden = !ph.running;
    r.barWrap.hidden = !ph.running;
    const p = ph.progress;
    r.bar.style.width = ph.running && p && p.bytes_total ? `${Math.min(100, (100 * p.bytes_done) / p.bytes_total)}%` : "0";
    const lr = ph.last_run;
    r.status.textContent = ph.running ? progressText(p) : lastRunText(lr);
    r.status.classList.toggle("err", !ph.running && !!lr && lr.status === "error");
    const errs = (!ph.running && lr && lr.errors) || [];
    r.errors.hidden = !errs.length;
    if (errs.length) r.errors.replaceChildren(el("summary", { textContent: `${errs.length} problem${errs.length > 1 ? "s" : ""}` }),
      el("ul", {}, ...errs.map((e) => el("li", { textContent: e }))));
  }

  async function refresh() {
    clearTimeout(timer);
    let list = [];
    try {
      list = (await api("GET", "/api/backup")).phones;
    } catch (e) {
      toast(`Could not load phones: ${e.message}`, true);
    }
    const main = $("bkMain");
    if (!list.length) {
      main.replaceChildren(el("div", { className: "bk-empty", textContent: "No phones yet. Connect a phone by USB or pair one remotely." }));
      cards.clear();
    } else {
      if (!cards.size) main.replaceChildren();
      const keys = new Set(list.map((p) => p.phone_key));
      for (const [k, c] of cards) if (!keys.has(k)) { c.root.remove(); cards.delete(k); }
      for (const ph of list) {
        let c = cards.get(ph.phone_key);
        if (!c) { c = build(ph); cards.set(ph.phone_key, c); main.append(c.root); }
        render(c, ph);
      }
    }
    timer = setTimeout(refresh, list.some((p) => p.running) ? 1000 : 5000);
  }

  function selectionOf(ph) {
    const c = cards.get(ph.phone_key);
    return { presets: PRESETS.map(([k]) => k).filter((k) => c.r.checks[k].checked), custom: (c.ph.selection || {}).custom || [] };
  }
  async function save(ph, sel = selectionOf(ph)) {
    try {
      await api("PUT", `/api/backup/${encodeURIComponent(ph.device_id)}/selection`, sel);
    } catch (e) { toast(e.message, true); }
    refresh();
  }
  async function addCustom(ph) {
    const c = cards.get(ph.phone_key);
    const path = c.r.input.value.trim();
    if (!path) return;
    try {
      await api("POST", `/api/backup/${encodeURIComponent(ph.device_id)}/custom`, { paths: [path] });
      c.r.input.value = "";
    } catch (e) { toast(e.message, true); }
    refresh();
  }
  function removeCustom(ph, path) {
    const sel = selectionOf(ph);
    sel.custom = sel.custom.filter((p) => p !== path);
    save(ph, sel);
  }
  async function run(ph, action) {
    try {
      await api("POST", `/api/backup/${encodeURIComponent(ph.device_id)}/${action}`);
    } catch (e) { toast(e.message, true); }
    refresh();
  }

  refresh();
})();
