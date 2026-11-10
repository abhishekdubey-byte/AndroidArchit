// PowerBoosters Commander HQ. All phone-supplied text is escaped before it touches the DOM.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (t) => t ? new Date(t * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
const human = (b) => b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : b > 1024 ? Math.round(b / 1024) + ' KB' : b + ' B';
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/-/g, '').slice(0, 24);

async function api(path, opts = {}) {
  const r = await fetch('/api/pb' + path, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body && typeof opts.body !== 'string' && !(opts.body instanceof Blob) ? JSON.stringify(opts.body) : opts.body });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function toast(html) {
  const d = document.createElement('div'); d.innerHTML = html; $('#toast').append(d); setTimeout(() => d.remove(), 5000);
}
const fail = (e) => toast('⚠ ' + esc(e.message || e));

const S = { tab: 'chat', convs: [], active: null, msgs: [], reply: null, chatFiles: [], refFiles: [], members: [], missions: [],
  review: null, reviewAttempt: null, lb: 'global', profile: null };

// ---------- theme ----------
(function theme() {
  const saved = (() => { try { return localStorage.getItem('pbTheme'); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $('#themeBtn').onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const next = cur === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('pbTheme', next); } catch {}
  };
})();

// ---------- tabs ----------
$$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  S.tab = t;
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  $$('.pane').forEach(p => p.classList.toggle('on', p.id === 'pane-' + t));
  ({ chat: loadConvs, missions: loadMissions, review: loadQueue, squad: loadSquad, alerts: loadAlerts })[t]();
}

// ---------- uploads (chunked + resumable, SHA-256 verified on the laptop) ----------
async function sha256(file) {
  if (!crypto.subtle || file.size > 300 * 1048576) return '';
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function uploadFile(file, purpose, deviceId, onProgress) {
  const st = await api('/files', { method: 'POST', body: { name: file.name, mime: file.type, size: file.size,
    sha256: await sha256(file), purpose, upload_id: uid(), device_id: deviceId || null } });
  let got = st.received, id = st.file_id, tries = 0;
  while (st.status !== 'ready' && got < file.size) {
    const chunk = file.slice(got, got + 4 * 1048576);
    try {
      const r = await api(`/files/${id}?offset=${got}`, { method: 'PUT', body: chunk, headers: { 'Content-Type': 'application/octet-stream' } });
      got = r.received; tries = 0; onProgress && onProgress(got / file.size);
      if (r.status === 'ready') return r.file;
    } catch (e) { if (++tries > 5) throw e; await new Promise(r => setTimeout(r, 1000 * tries)); }
  }
  onProgress && onProgress(1);
  return st.file;
}
function pendingChip(name) {
  const d = document.createElement('div'); d.className = 'pf'; d.innerHTML = `${esc(name)}<progress max="1" value="0"></progress>`; return d;
}

function attachmentHtml(f, big = false) {
  const url = `/api/pb/files/${encodeURIComponent(f.id)}`;
  if (f.status && f.status !== 'ready') return `<div class="att muted">⏳ ${esc(f.name)} uploading…</div>`;
  if (f.kind === 'image') return `<div class="att"><img src="${url}" alt="${esc(f.name)}" data-zoom loading="lazy"></div>`;
  if (f.kind === 'video') return `<div class="att"><video src="${url}" controls preload="metadata"></video></div>`;
  if (f.kind === 'audio') return `<div class="att"><audio src="${url}" controls preload="metadata"></audio></div>`;
  if (f.mime === 'application/pdf' && big) return `<div class="att"><iframe src="${url}" title="${esc(f.name)}"></iframe><a class="file" href="${url}?download=true">⬇ ${esc(f.name)} · ${human(f.size)}</a></div>`;
  return `<div class="att"><a class="file" href="${url}?download=true">📄 ${esc(f.name)} <small>${human(f.size)}</small></a></div>`;
}
document.addEventListener('click', (e) => {
  if (e.target.matches('img[data-zoom]')) {
    const lb = document.createElement('div'); lb.className = 'lightbox'; lb.innerHTML = `<img src="${e.target.src}">`;
    lb.onclick = () => lb.remove(); document.body.append(lb);
  }
});

// =================== CHAT ===================
async function loadConvs() {
  try { S.convs = (await api('/conversations')).conversations; } catch (e) { return fail(e); }
  renderConvs();
  if (!S.active && S.convs.length) openConv(S.convs[0].device_id);
}
function renderConvs() {
  const q = $('#convSearch').value.toLowerCase();
  $('#convs').innerHTML = S.convs.filter(c => c.name.toLowerCase().includes(q)).map(c => `
    <div class="conv ${c.device_id === S.active ? 'on' : ''}" data-d="${esc(c.device_id)}">
      <div class="avatar ${c.online ? 'online' : ''}"><span>${esc(c.name.slice(0, 1).toUpperCase())}</span></div>
      <div class="meta"><b>${esc(c.name)}</b><small class="${c.unread ? 'unread' : ''}">${esc(c.last_text || (c.online ? 'Online' : 'Offline'))}</small></div>
      ${c.unread ? '<span class="dot"></span>' : ''}
    </div>`).join('') || '<p class="muted" style="padding:14px">No phones have connected yet.</p>';
  $$('#convs .conv').forEach(el => el.onclick = () => openConv(el.dataset.d));
  const unread = S.convs.reduce((a, c) => a + (c.unread || 0), 0);
  $('#badgeChat').textContent = unread || '';
}
$('#convSearch').oninput = renderConvs;

async function openConv(deviceId, q = '') {
  S.active = deviceId; S.reply = null; $('#replyBar').classList.add('hidden');
  const c = S.convs.find(x => x.device_id === deviceId) || { name: deviceId };
  $('#chatHead').innerHTML = `<div class="avatar ${c.online ? 'online' : ''}"><span>${esc(c.name.slice(0, 1).toUpperCase())}</span></div>
    <div class="grow"><b>${esc(c.name)}</b><div class="muted" style="font-size:12px">${c.online ? 'Active now' : 'Offline — messages are delivered when it reconnects'}</div></div>
    <span class="mood-tag" id="moodTag"></span><button class="ghost" id="searchToggle">🔍</button>`;
  $('#searchToggle').onclick = () => $('#msgSearchBar').classList.toggle('hidden');
  try {
    const r = await api(`/conversations/${encodeURIComponent(deviceId)}/messages?q=${encodeURIComponent(q)}`);
    S.msgs = r.messages; S.conv = r.conversation;
  } catch (e) { return fail(e); }
  renderMsgs(true);
  renderConvs();
  markRead();
}
$('#msgSearch').oninput = (e) => { clearTimeout(S.st); S.st = setTimeout(() => openConv(S.active, e.target.value), 300); };

function markRead() {
  const last = S.msgs[S.msgs.length - 1];
  if (!last || !S.active) return;
  const c = S.convs.find(x => x.device_id === S.active); if (c) c.unread = 0;
  api(`/conversations/${encodeURIComponent(S.active)}/read`, { method: 'POST', body: { seq: last.seq } }).catch(() => {});
  renderConvs();
}

function renderMsgs(scroll) {
  const box = $('#msgs');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const lastMine = [...S.msgs].reverse().find(m => m.sender === 'commander');
  box.innerHTML = S.msgs.map(m => {
    const mine = m.sender === 'commander';
    const reacts = Object.values(m.reactions || {}).join('');
    return `<div class="msg ${mine ? 'mine' : ''}" data-id="${esc(m.id)}">
      <div class="tools">${['❤️', '🔥', '👍', '😂'].map(e => `<button data-react="${e}">${e}</button>`).join('')}
        <button data-reply title="Reply">↩</button><button data-fwd title="Forward">↪</button></div>
      <div class="bubble">${m.forwarded ? '<div class="quote">↪ Forwarded</div>' : ''}${
        m.reply ? `<div class="quote">${esc(m.reply.text || 'attachment')}</div>` : ''}${
        (m.attachments || []).map(f => attachmentHtml(f)).join('')}${esc(m.text)}</div>
      ${reacts ? `<span class="reacts">${esc(reacts)}</span>` : ''}
      <span class="time">${fmt(m.created_at)}${mine && m === lastMine ? (m.read ? ' · Seen' : m.delivered ? ' · Delivered' : ' · Sent') : ''}</span>
    </div>`;
  }).join('') || '<p class="muted" style="text-align:center;margin-top:40px">No messages yet. Say hi 👋</p>';
  if (scroll || atBottom) box.scrollTop = box.scrollHeight;
  PbMood.apply($('#pane-chat'), S.msgs);
}
$('#msgs').addEventListener('click', async (e) => {
  const el = e.target.closest('.msg'); if (!el) return;
  const m = S.msgs.find(x => x.id === el.dataset.id);
  if (e.target.dataset.react) { try { upsertMsg(await api(`/messages/${m.id}/react`, { method: 'POST', body: { emoji: e.target.dataset.react } })); } catch (er) { fail(er); } }
  if ('reply' in e.target.dataset) { S.reply = m; $('#replyBar').classList.remove('hidden');
    $('#replyBar').innerHTML = `<span>↩ Replying to: ${esc(m.text || 'attachment')}</span><button class="ghost" id="cancelReply">✕</button>`;
    $('#cancelReply').onclick = () => { S.reply = null; $('#replyBar').classList.add('hidden'); }; $('#chatInput').focus(); }
  if ('fwd' in e.target.dataset) {
    const names = S.convs.filter(c => c.device_id !== S.active).map((c, i) => `${i + 1}. ${c.name}`).join('\n');
    const pick = prompt('Forward to which members? Enter numbers separated by commas, or "all".\n\n' + names);
    if (!pick) return;
    const others = S.convs.filter(c => c.device_id !== S.active);
    const ids = pick.trim().toLowerCase() === 'all' ? others.map(c => c.device_id)
      : pick.split(',').map(n => others[parseInt(n) - 1]).filter(Boolean).map(c => c.device_id);
    try { await api(`/messages/${m.id}/forward`, { method: 'POST', body: { device_ids: ids } }); toast(`↪ Forwarded to ${ids.length} member(s)`); } catch (er) { fail(er); }
  }
});
function upsertMsg(m) {
  const i = S.msgs.findIndex(x => x.id === m.id);
  if (i >= 0) S.msgs[i] = m; else S.msgs.push(m);
  S.msgs.sort((a, b) => a.seq - b.seq);
  renderMsgs();
}

$('#chatFile').onchange = async (e) => {
  for (const f of e.target.files) {
    const chip = pendingChip(f.name); $('#chatFiles').append(chip);
    const entry = { name: f.name, file: null }; S.chatFiles.push(entry);
    uploadFile(f, 'chat', S.active, p => chip.querySelector('progress').value = p)
      .then(r => { entry.file = r; chip.querySelector('progress').replaceWith(Object.assign(document.createElement('small'), { textContent: ' ✓ ready' })); })
      .catch(er => { chip.remove(); S.chatFiles = S.chatFiles.filter(x => x !== entry); fail(er); });
  }
  e.target.value = '';
};
let typingT = 0;
$('#chatInput').oninput = () => {
  if (!S.active || Date.now() - typingT < 3000) return; typingT = Date.now();
  api(`/conversations/${encodeURIComponent(S.active)}/typing?typing=true`, { method: 'POST' }).catch(() => {});
};
$('#composer').onsubmit = async (e) => {
  e.preventDefault();
  if (!S.active) return;
  if (S.chatFiles.some(f => !f.file)) return toast('⏳ Wait for the attachments to finish uploading');
  const text = $('#chatInput').value.trim();
  if (!text && !S.chatFiles.length) return;
  try {
    const m = await api(`/conversations/${encodeURIComponent(S.active)}/messages`, { method: 'POST',
      body: { text, client_id: uid(), reply_to: S.reply?.id || '', attachments: S.chatFiles.map(f => f.file.id) } });
    $('#chatInput').value = ''; S.chatFiles = []; $('#chatFiles').innerHTML = ''; S.reply = null; $('#replyBar').classList.add('hidden');
    upsertMsg(m);
  } catch (er) { fail(er); }
};

// =================== MISSIONS ===================
async function loadMembers() {
  try { S.members = (await api('/members')).members; } catch (e) { fail(e); }
  const opts = S.members.map(m => `<option value="${esc(m.device_id)}">${m.online ? '🟢' : '⚪'} ${esc(m.name)}</option>`).join('');
  $('#missionForm [name=target]').innerHTML = opts;
  const f = $('#reviewFilters [name=device_id]'); const v = f.value;
  f.innerHTML = '<option value="">All devices</option>' + opts; f.value = v;
}
async function loadMissions() {
  await loadMembers();
  try { S.missions = (await api('/missions')).missions; } catch (e) { return fail(e); }
  const order = ['completed', 'submitted', 'in_progress', 'revision_required', 'assigned', 'failed'];
  const colors = { completed: 'var(--green)', submitted: 'var(--amber)', in_progress: 'var(--blue)', revision_required: 'var(--pink)', assigned: 'var(--cyan)', failed: 'var(--red)' };
  $('#missionList').innerHTML = S.missions.map(m => {
    const p = m.progress || {}; const total = p.total || 0;
    return `<div class="mcard" data-id="${esc(m.id)}">
      <header><span class="chip ${esc(m.priority)}">${esc(m.priority)}</span>
        <span class="chip">${m.distribution === 'targeted' ? '🎯 targeted' : '🌐 global'}</span>
        <span class="chip ${m.status === 'draft' ? 'wait' : 'ok'}">${esc(m.status)}</span>
        <span class="xp">+${m.xp_reward}${m.bonus_xp ? ' +' + m.bonus_xp : ''} XP</span></header>
      <h3>${esc(m.title)}</h3><div class="muted">${esc(m.description).slice(0, 160)}</div>
      ${m.status === 'draft' ? `<div class="actions"><button class="ghost" data-edit>Edit</button><button class="ghost" data-del>Delete</button><button class="primary" data-pub>🚀 Deploy</button></div>` : `
      <div class="bar">${order.map(s => p[s] ? `<div style="width:${100 * p[s] / total}%;background:${colors[s]}" title="${s}: ${p[s]}"></div>` : '').join('')}</div>
      <div class="legend">${order.filter(s => p[s]).map(s => `<span style="color:${colors[s]}">● ${s.replace('_', ' ')} ${p[s]}</span>`).join('')}<span>· ${total} member(s)</span>
        ${m.deadline ? `<span>⏱ ${fmt(m.deadline)}</span>` : ''}<a href="#" data-more>details</a></div>
      <div class="assignees"></div>`}
    </div>`;
  }).join('') || '<p class="muted">No missions yet — deploy the first one.</p>';
}
$('#missionList').addEventListener('click', async (e) => {
  const card = e.target.closest('.mcard'); if (!card) return;
  const id = card.dataset.id;
  try {
    if ('pub' in e.target.dataset) { await api(`/missions/${id}/publish`, { method: 'POST' }); toast('🚀 Mission deployed'); loadMissions(); }
    if ('del' in e.target.dataset && confirm('Delete this draft?')) { await api(`/missions/${id}`, { method: 'DELETE' }); loadMissions(); }
    if ('edit' in e.target.dataset) fillForm(S.missions.find(m => m.id === id));
    if ('more' in e.target.dataset) {
      e.preventDefault();
      const m = await api(`/missions/${id}`);
      card.querySelector('.assignees').innerHTML = m.assignments.map(a => `<div><span>${esc(a.name)}</span><span>${esc(a.status.replace('_', ' '))} · attempts ${a.attempts_used}${a.delivered_at ? '' : ' · ⏳ not delivered yet'}</span></div>`).join('');
      card.classList.toggle('open');
    }
  } catch (er) { fail(er); }
});

const mf = $('#missionForm');
mf.distribution.onchange = () => $('.target-wrap').classList.toggle('hidden', mf.distribution.value !== 'targeted');
function fillForm(m) {
  mf.reset(); S.refFiles = []; $('#refList').innerHTML = '';
  mf.id.value = m?.id || ''; $('#mfTitle').textContent = m ? 'Edit draft' : 'Deploy a mission';
  if (!m) { $('.target-wrap').classList.add('hidden'); return; }
  for (const k of ['title', 'description', 'instructions', 'distribution', 'priority', 'xp_reward', 'bonus_xp', 'max_attempts', 'max_attachment_mb', 'deliverables', 'submission_instructions'])
    if (mf[k]) mf[k].value = m[k] ?? '';
  mf.allowed_formats.value = (m.allowed_formats || []).join(', ');
  if (m.deadline) { const d = new Date(m.deadline * 1000); mf.deadline.value = new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); }
  if (m.distribution === 'targeted') { $('.target-wrap').classList.remove('hidden'); mf.target.value = m.targets[0]; }
  S.refFiles = (m.reference_files || []).map(f => ({ name: f.name, file: f }));
  S.refFiles.forEach(r => { const c = pendingChip(r.name); c.querySelector('progress').remove(); $('#refList').append(c); });
}
$('#newMission').onclick = () => fillForm(null);
$('#refFiles').onchange = (e) => {
  for (const f of e.target.files) {
    const chip = pendingChip(f.name); $('#refList').append(chip);
    const entry = { name: f.name, file: null }; S.refFiles.push(entry);
    uploadFile(f, 'reference', null, p => chip.querySelector('progress').value = p)
      .then(r => { entry.file = r; }).catch(er => { chip.remove(); S.refFiles = S.refFiles.filter(x => x !== entry); fail(er); });
  }
  e.target.value = '';
};
mf.onsubmit = async (e) => {
  e.preventDefault();
  const mode = e.submitter?.dataset.mode || 'draft';
  if (S.refFiles.some(f => !f.file)) return toast('⏳ Reference files are still uploading');
  const body = Object.fromEntries(new FormData(mf));
  body.targets = body.distribution === 'targeted' ? [body.target] : [];
  body.deadline = body.deadline ? new Date(body.deadline).getTime() / 1000 : null;
  body.reference_files = S.refFiles.map(f => f.file.id);
  ['xp_reward', 'bonus_xp', 'max_attempts', 'max_attachment_mb'].forEach(k => body[k] = Number(body[k] || 0));
  try {
    let m;
    if (body.id) { m = await api(`/missions/${body.id}`, { method: 'PUT', body }); if (mode === 'publish') await api(`/missions/${m.id}/publish`, { method: 'POST' }); }
    else m = await api('/missions', { method: 'POST', body: { ...body, publish: mode === 'publish' } });
    toast(mode === 'publish' ? `🚀 Deployed to ${m.assignments?.length || ''} member(s)` : '💾 Draft saved');
    fillForm(null); loadMissions();
  } catch (er) { fail(er); }
};

// =================== VERIFICATION CENTRE ===================
$$('#reviewFilters [name]').forEach(el => el.onchange = loadQueue);
async function loadQueue() {
  if (!S.members.length) await loadMembers();
  if (!S.missions.length) try { S.missions = (await api('/missions')).missions; } catch {}
  const mfSel = $('#reviewFilters [name=mission_id]'); const mv = mfSel.value;
  mfSel.innerHTML = '<option value="">All missions</option>' + S.missions.filter(m => m.status !== 'draft').map(m => `<option value="${esc(m.id)}">${esc(m.title)}</option>`).join('');
  mfSel.value = mv;
  const q = new URLSearchParams();
  $$('#reviewFilters [name]').forEach(el => {
    if (!el.value) return;
    q.set(el.name, el.type === 'date' ? new Date(el.value + (el.name === 'until' ? 'T23:59:59' : 'T00:00:00')).getTime() / 1000 : el.value);
  });
  let rows; try { rows = (await api('/reviews?' + q)).submissions; } catch (e) { return fail(e); }
  $('#queue').innerHTML = rows.map(s => `<div class="qitem ${s.id === S.review ? 'on' : ''}" data-id="${esc(s.id)}">
      <div class="avatar"><span>${esc(s.member_name.slice(0, 1))}</span></div>
      <div class="meta"><b>${esc(s.mission_title)}</b><small>${esc(s.member_name)} · attempt ${s.attempt} · ${fmt(s.submitted_at)} · 📎 ${s.attachment_count}</small></div>
      <span class="chip ${esc(s.priority)}">${esc(s.priority)}</span>
      <span class="chip ${s.status === 'approved' ? 'ok' : s.status === 'pending' ? 'wait' : 'bad'}">${esc(s.status.replace('_', ' '))}</span></div>`).join('')
    || '<p class="muted">Nothing here. 🎉</p>';
  $$('#queue .qitem').forEach(el => el.onclick = () => openReview(el.dataset.id));
  refreshPendingBadge();
}
async function refreshPendingBadge() {
  try { const n = (await api('/reviews?status=pending')).submissions.length; $('#badgeReview').textContent = n || ''; } catch {}
}
async function openReview(id) {
  S.review = id; $$('#queue .qitem').forEach(el => el.classList.toggle('on', el.dataset.id === id));
  let d; try { d = await api(`/reviews/${id}`); } catch (e) { return fail(e); }
  const s = d.submission, m = d.mission;
  const prev = d.history.filter(h => h.attempt < s.attempt).pop();
  const histRows = d.history.map(h => `<tr><td>${h.attempt}</td><td>${esc(h.status.replace('_', ' '))}</td><td>${esc((h.reviews.at(-1) || {}).feedback || '—')}</td><td>${fmt(h.submitted_at)}</td><td>${esc((h.reviews.at(-1) || {}).reviewer || '')}</td></tr>`).join('');
  const subHtml = (x) => `<div class="box"><h4>Description</h4>${esc(x.text) || '<span class="muted">—</span>'}</div>
     ${x.comment ? `<div class="box"><h4>Comments</h4>${esc(x.comment)}</div>` : ''}
     <div class="att-grid">${x.attachments.map(f => attachmentHtml(f, true)).join('') || '<span class="muted">No attachments</span>'}</div>`;
  $('#reviewDetail').innerHTML = `
    <header style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span class="chip ${esc(m.priority)}">${esc(m.priority)}</span><span class="chip">attempt ${s.attempt} of ${m.max_attempts}</span>
      <span class="chip ${s.status === 'approved' ? 'ok' : s.status === 'pending' ? 'wait' : 'bad'}">${esc(s.status.replace('_', ' '))}</span>
      <span class="xp">+${m.xp_reward}${m.bonus_xp ? ' +' + m.bonus_xp : ''} XP</span></header>
    <h3 style="margin:10px 0 2px">${esc(m.title)}</h3>
    <div class="muted">${esc(d.member.name)} · submitted ${fmt(s.submitted_at)}</div>
    <div class="review-grid" style="margin-top:14px">
      <div><h2>Mission instructions</h2>
        <div class="box"><h4>Instructions</h4>${esc(m.instructions || m.description) || '—'}</div>
        <div class="box"><h4>Expected deliverables</h4>${esc(m.deliverables) || '—'}</div>
        <div class="box"><h4>Formats</h4>${m.allowed_formats.length ? esc(m.allowed_formats.join(', ')) : 'any'} · max ${m.max_attachment_mb} MB</div></div>
      <div><h2>Submitted work</h2>${subHtml(s)}</div>
    </div>
    ${prev ? `<details style="margin-top:10px"><summary>⇆ Compare with attempt ${prev.attempt}</summary><div class="compare"><div><h2>Attempt ${prev.attempt}</h2>${subHtml(prev)}</div><div><h2>Attempt ${s.attempt}</h2>${subHtml(s)}</div></div></details>` : ''}
    <h2>Review history</h2>
    <table class="history"><tr><th>#</th><th>Result</th><th>Feedback</th><th>Submitted</th><th>Reviewer</th></tr>${histRows}</table>
    ${s.status === 'pending' ? `<div class="review-actions" style="margin-top:14px">
      <textarea id="fb" rows="3" placeholder="Feedback / reason (required to reject or request a revision)"></textarea>
      <div class="row" style="align-items:center"><label class="muted" style="flex:0 0 auto">Bonus-quality XP</label><input id="bonusXp" type="number" min="0" value="0" style="max-width:110px"></div>
      <div class="actions">
        <button class="warn" data-act="revision">✏️ Request revision</button>
        <button class="danger" data-act="reject">❌ Reject &amp; request correction</button>
        <button class="ok" data-act="approve">✅ Approve mission</button>
      </div></div>` : ''}`;
  $$('#reviewDetail [data-act]').forEach(b => b.onclick = async () => {
    const act = b.dataset.act;
    const feedback = $('#fb').value.trim();
    if (act !== 'approve' && !feedback) return toast('✍️ Tell the member what needs to change');
    $$('#reviewDetail [data-act]').forEach(x => x.disabled = true);   // no double-approve
    try {
      const r = await api(`/reviews/${id}`, { method: 'POST', body: { action: act, feedback, bonus_xp: Number($('#bonusXp').value || 0) } });
      toast(act === 'approve' ? `✅ Approved · +${r.xp_total || 0} XP to ${esc(d.member.name)}` : act === 'reject' ? '❌ Rejected — member notified' : '✏️ Revision requested');
      loadQueue(); openReview(id);
    } catch (e) { fail(e); $$('#reviewDetail [data-act]').forEach(x => x.disabled = false); }
  });
}

// =================== SQUAD ===================
$$('#lbSeg button').forEach(b => b.onclick = () => { S.lb = b.dataset.p; $$('#lbSeg button').forEach(x => x.classList.toggle('on', x === b)); loadSquad(); });
async function loadSquad() {
  try {
    const [lb, pol, led] = await Promise.all([api('/leaderboard?period=' + S.lb), api('/policy'), api('/ledger' + (S.profile ? '?device_id=' + encodeURIComponent(S.profile) : ''))]);
    $('#leaderboard').innerHTML = lb.rows.map(r => `<div class="lb" data-d="${esc(r.device_id)}"><span class="rank">${['🥇', '🥈', '🥉'][r.rank - 1] || '#' + r.rank}</span>
      <div class="grow"><b>${esc(r.name)}</b><small>Level ${r.level} · ${r.completed} missions</small></div><span class="xp">${r.xp} XP</span></div>`).join('') || '<p class="muted">No members yet.</p>';
    $$('#leaderboard .lb').forEach(el => el.onclick = () => { S.profile = el.dataset.d; loadSquad(); });
    for (const k in pol) if ($('#policyForm')[k]) $('#policyForm')[k].value = pol[k];
    $('#ledger').innerHTML = `<table><tr><th>When</th><th>Member</th><th>XP</th><th>Type</th><th>Reason</th><th>Tx</th></tr>${led.transactions.map(t =>
      `<tr><td>${fmt(t.created_at)}</td><td>${esc(t.name || '')}</td><td style="color:var(--green)">+${t.amount}</td><td>${esc(t.tx_type)}</td><td>${esc(t.reason)}</td><td class="muted" title="${esc(t.approval_ref || '')}">${esc(t.id)}</td></tr>`).join('')}</table>`;
    if (S.profile) renderProfile(await api('/members/' + encodeURIComponent(S.profile)));
  } catch (e) { fail(e); }
}
function renderProfile(p) {
  const pct = Math.max(0, Math.min(100, 100 * (p.xp - p.level_floor) / Math.max(1, p.next_level_xp - p.level_floor)));
  $('#profile').innerHTML = `<div style="display:flex;gap:14px;align-items:center">
      <div class="avatar ${p.online ? 'online' : ''}" style="width:64px;height:64px"><span style="font-size:24px">${p.level}</span></div>
      <div><h3 style="margin:0">${esc(p.name)}</h3><div class="muted">Level ${p.level} · ${p.xp} XP · ${p.xp_to_next} XP to level ${p.level + 1}</div></div></div>
    <div class="xpbar"><div style="width:${pct}%"></div></div>
    <div class="stat-row"><div><b>${p.completed_missions}</b>completed</div><div><b>${p.assigned_missions}</b>assigned</div><div><b>${p.rejected_attempts}</b>revisions</div></div>
    <div class="badges">${p.achievements.map(a => `<div class="badge ${a.unlocked_at ? '' : 'locked'}" title="${esc(a.description)}"><b>${a.icon}</b>${esc(a.name)}</div>`).join('')}</div>
    <form id="bonusForm" class="row" style="align-items:flex-end"><label class="muted">Manual bonus XP<input name="amount" type="number" min="1" required></label>
      <label class="muted" style="flex:2">Reason<input name="reason" required placeholder="Exceptional contribution"></label><button class="primary">⭐ Grant</button></form>`;
  $('#bonusForm').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    try { await api('/bonus', { method: 'POST', body: { device_id: p.device_id, amount: Number(f.amount.value), reason: f.reason.value, request_id: uid() } });
      toast(`⭐ +${esc(f.amount.value)} XP granted`); loadSquad(); } catch (er) { fail(er); }
  };
}
$('#policyForm').onsubmit = async (e) => {
  e.preventDefault();
  const body = Object.fromEntries([...new FormData(e.target)].map(([k, v]) => [k, Number(v)]));
  try { await api('/policy', { method: 'PUT', body }); toast('Reward policy saved'); } catch (er) { fail(er); }
};

// =================== ALERTS ===================
async function loadAlerts() {
  let list; try { list = (await api('/notifications')).notifications; } catch (e) { return fail(e); }
  $('#badgeAlerts').textContent = list.filter(n => !n.read).length || '';
  $('#alerts').innerHTML = list.map(n => `<div class="alert ${n.read ? '' : 'unread'}"><b>${esc(n.title)}</b><div>${esc(n.body)}</div><small>${fmt(n.created_at)}</small></div>`).join('') || '<p class="muted">No alerts yet.</p>';
}
$('#readAll').onclick = async () => { await api('/notifications/read', { method: 'POST' }).catch(fail); loadAlerts(); };

// =================== live events ===================
function connect() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/dashboard' + location.search);
  ws.onopen = () => { $('#live').textContent = '● live'; $('#live').classList.add('on'); };
  ws.onclose = () => { $('#live').textContent = '● reconnecting'; $('#live').classList.remove('on'); setTimeout(connect, 2000); };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type !== 'PB_EVENT') return;
    const d = msg.data;
    switch (msg.event) {
      case 'message':
        if (d.device_id === S.active) { upsertMsg(d); if (d.sender === 'member' && S.tab === 'chat' && document.hasFocus()) markRead(); }
        if (S.tab === 'chat') loadConvs(); break;
      case 'typing':
        if (d.device_id === S.active) { $('#typing').classList.toggle('hidden', !d.typing); clearTimeout(S.tt); S.tt = setTimeout(() => $('#typing').classList.add('hidden'), 5000); } break;
      case 'receipt':
        if (d.device_id === S.active) { S.msgs.forEach(m => { if (m.sender === 'commander') { m.read = d.member_read_seq >= m.seq; m.delivered = d.member_delivered_seq >= m.seq; } }); renderMsgs(); } break;
      case 'presence':
        { const c = S.convs.find(x => x.device_id === d.device_id); if (c) { c.online = d.online; renderConvs(); } } break;
      case 'notification':
        toast(`<b>${esc(d.title)}</b><br><small>${esc(d.body)}</small>`);
        $('#badgeAlerts').textContent = (Number($('#badgeAlerts').textContent) || 0) + 1;
        if (Notification?.permission === 'granted' && document.hidden) new Notification(d.title, { body: d.body });
        break;
      case 'submission': refreshPendingBadge(); if (S.tab === 'review') loadQueue(); break;
      case 'assignment': case 'mission': case 'review': if (S.tab === 'missions') loadMissions(); if (S.tab === 'review') loadQueue(); break;
    }
  };
}
document.addEventListener('focus', markRead, true);
if (window.Notification && Notification.permission === 'default') document.addEventListener('click', () => Notification.requestPermission(), { once: true });
connect();
showTab('chat');
refreshPendingBadge();
api('/notifications').then(r => $('#badgeAlerts').textContent = r.notifications.filter(n => !n.read).length || '').catch(() => {});
