// Phone Permissions page: one card per phone, fresh from the app (or ADB over USB); read-only.
(() => {
  const $ = (id) => document.getElementById(id);
  const ICON = { on: "✅", off: "❌", unknown: "❔", "n/a": "➖" };
  const SOURCE = { app: "📡 reported by the app just now", adb: "🔌 read over USB (ADB)", "last known": "🕓 last report (phone offline)" };

  function el(tag, props = {}, ...kids) {
    const n = Object.assign(document.createElement(tag), props);
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }
  function toast(msg) {
    const t = el("div", { className: "fm-toast err", textContent: msg });
    $("fmToasts").append(t);
    setTimeout(() => t.remove(), 6000);
  }

  function card(p) {
    const missing = p.rows.filter((r) => r.state === "off").length;
    const c = el("section", { className: "pm-card" });
    c.append(el("div", { className: "pm-head" },
      el("span", { className: "pm-name", textContent: p.name }),
      p.app_version ? el("span", { className: "pm-badge", textContent: `v${p.app_version}${p.edition === "lite" ? " Lite" : ""}` }) : null,
      p.app_running === false ? el("span", { className: "pm-badge", textContent: "⚠ app not running", style: "color:#fda4af" }) : null));
    c.append(el("div", { className: "pm-sub", textContent: SOURCE[p.source] || p.source }));
    if (p.message) c.append(el("div", { className: "pm-msg", textContent: p.message }));
    if (p.app_running === false && p.source === "adb")
      c.append(el("div", { className: "pm-msg", textContent: "The app is stopped — open Operator Companion on the phone (on Realme/Oppo/Vivo/Xiaomi also allow background activity and auto launch)." }));
    const unknown = p.rows.filter((r) => r.state === "unknown").length;
    if (p.rows.length) {
      const parts = [];
      if (missing) parts.push(`${missing} not granted — see the steps below`);
      if (unknown) parts.push(`${unknown} unknown — this app version doesn't report them; it updates itself to the latest when it connects`);
      c.append(el("div", { className: "pm-sum", textContent: parts.join(" · ") || "Everything this phone needs is granted" }));
    }
    // not granted first, then unknown, then granted
    const order = { off: 0, unknown: 1, "n/a": 2, on: 3 };
    for (const r of [...p.rows].sort((a, b) => order[a.state] - order[b.state])) {
      const row = el("div", { className: "pm-row" },
        el("span", { className: "pm-icon", textContent: ICON[r.state] || "❔", title: r.state }),
        el("div", {}, el("div", { className: "pm-title", textContent: r.title }), el("div", { className: "pm-unlocks", textContent: r.unlocks })));
      if (r.note) row.append(el("div", { className: "pm-note", textContent: r.note }));
      if (r.state === "off") row.append(el("div", { className: "pm-fix", textContent: `How to fix: ${r.fix}` }));
      if (r.key === "screen_share" && r.state !== "on" && p.usb) {
        const b = el("button", { className: "pm-btn", textContent: "Pre-approve over USB",
          title: "Lets the app share this phone's screen with the dashboard without the “Start now” prompt. "
            + "The phone still shows its cast icon while sharing." });
        b.onclick = async () => {
          b.disabled = true; b.textContent = "Setting…";
          try {
            const res = await fetch(`/api/permissions/${encodeURIComponent(p.device_id)}/screen-share`, { method: "POST" });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
            load();
          } catch (e) { toast(`Could not pre-approve: ${e.message}`); b.disabled = false; b.textContent = "Pre-approve over USB"; }
        };
        row.append(b);
      }
      c.append(row);
    }
    return c;
  }

  async function load() {
    const btn = $("pmRefresh");
    btn.disabled = true;
    btn.textContent = "Checking…";
    try {
      const res = await fetch("/api/permissions");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const phones = (await res.json()).phones;
      $("pmMain").replaceChildren(...(phones.length ? phones.map(card)
        : [el("div", { className: "pm-empty", textContent: "No phones connected. Plug one in by USB or open the app on a paired phone." })]));
    } catch (e) {
      toast(`Could not check the phones: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "⟳ Check again";
    }
  }

  $("pmRefresh").onclick = load;
  load();
})();
