"use strict";

// ---- track colors by group, tinted by depth ------------------------------
const GROUP_COLOR = {
  mix:    [210, 215, 225],
  drums:  [231, 76,  60],
  bass:   [52,  152, 219],
  vocals: [46,  204, 113],
  other:  [243, 156, 18],
  guitar: [155, 89,  182],
  piano:  [26,  188, 156],
};
function trackColor(t) {
  const c = GROUP_COLOR[t.group] || [180, 180, 180];
  if (t.depth === 0) return c;
  return c.map(v => Math.round(v * 0.6 + 90 * 0.4));
}
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const STEMS = ["drums", "bass", "vocals", "other"];

// ---- state ---------------------------------------------------------------
const S = {
  songId: null, meta: null,
  view: { start: 0, end: 1 },
  mode: "wave",
  rows: [],            // every track (visible or not)
  visible: new Set(),
  collapsed: new Set(),  // parent group ids whose children are hidden
  show: { sections: true, beats: true, phrases: true },  // overlay marker toggles
  lock: false,         // scroll-lock: keep playhead centered while playing
  renderId: 0,
  _info: "",
};

const $ = s => document.querySelector(s);
const els = {
  song: $("#song"), mode: $("#mode"), play: $("#play"), fit: $("#fit"),
  zin: $("#zoomin"), zout: $("#zoomout"), lock: $("#lock-btn"),
  compBtn: $("#comp-btn"), compPanel: $("#comp-panel"),
  resetSm: $("#reset-sm"), toggleSub: $("#toggle-sub"), masterVol: $("#master-vol"),
  lyricsBtn: $("#lyrics-btn"), lyricsPanel: $("#lyrics-panel"),
  simBtn: $("#sim-btn"), simPanel: $("#sim-panel"), simFacet: $("#sim-facet"),
  simRefresh: $("#sim-refresh"), simClose: $("#sim-close"),
  simSeed: $("#sim-seed"), simResults: $("#sim-results"),
  simQ: $("#sim-q"), simQGo: $("#sim-q-go"),
  galBtn: $("#gal-btn"), galaxy: $("#galaxy"), galCanvas: $("#gal-canvas"),
  galTip: $("#gal-tip"), galStatus: $("#gal-status"), galClose: $("#gal-close"),
  radioBtn: $("#radio-btn"), tbarNext: $("#tbar-next"), tbarNextLabel: $("#tbar-next-label"),
  tbarJoin: $("#tbar-join"), tbarExit: $("#tbar-exit"),
  readout: $("#readout"), ruler: $("#ruler"), tracks: $("#tracks"),
  overlay: $("#overlay"), overlayWrap: $("#overlay-wrap"),
  playhead: $("#playhead"), cursor: $("#cursor"), scroll: $("#scroll"),
  addBtn: $("#add-btn"), addPanel: $("#add-panel"), dropzone: $("#dropzone"),
  urlInput: $("#url-input"), urlGo: $("#url-go"), jobs: $("#jobs"),
  dropOverlay: $("#drop-overlay"),
  liveBtn: $("#live-btn"), liveView: $("#live-view"), liveDevice: $("#live-device"),
  liveStart: $("#live-start"), liveStop: $("#live-stop"), liveLevel: $("#live-level"),
  liveHelpBtn: $("#live-help-btn"), liveHelp: $("#live-help"), liveCanvas: $("#live-canvas"),
};
const DPR = () => window.devicePixelRatio || 1;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmtTime = s => {
  if (!isFinite(s)) return "--";
  const m = Math.floor(s / 60), sec = s - m * 60;
  return `${m}:${sec.toFixed(s < 10 && m === 0 ? 3 : 2).padStart(s < 10 ? 6 : 5, "0")}`;
};
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }
// throttle: fires on the leading edge and at most once per `ms`, plus a trailing
// call — so continuous motion (e.g. scroll-lock) keeps refreshing instead of starving.
function throttle(fn, ms) {
  let last = 0, timer = null, lastArgs;
  return (...a) => {
    lastArgs = a; const now = Date.now(), wait = ms - (now - last);
    if (wait <= 0) { last = now; fn(...a); }
    else if (!timer) timer = setTimeout(() => { last = Date.now(); timer = null; fn(...lastArgs); }, wait);
  };
}

// ==========================================================================
// init
// ==========================================================================
async function init() {
  const songs = await fetch("/api/songs").then(r => r.json());
  els.song.innerHTML = songs.map(s => `<option value="${s.id}">${s.title}</option>`).join("");
  els.song.onchange = () => loadSong(els.song.value);

  // hosted preview: no server-side ingest, so hide "add music" + flag it as a demo
  fetch("/api/appinfo").then(r => r.json()).then(info => {
    if (!info.demo) return;
    document.body.classList.add("demo");
    if (els.addBtn) els.addBtn.style.display = "none";
    const badge = document.createElement("a");
    badge.id = "demo-badge";
    badge.href = "https://github.com/Harry-Geng/Audio-Visualizer";
    badge.target = "_blank"; badge.rel = "noopener";
    badge.textContent = "◆ demo — run it locally for your own library";
    const header = document.querySelector("header");
    if (header) header.appendChild(badge);
  }).catch(() => {});

  els.mode.querySelectorAll("button").forEach(b => b.onclick = () => {
    S.mode = b.dataset.mode;
    els.mode.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    S.rows.forEach(r => { r.cache = null; r.specCache = null; });
    requestRender(true);
  });
  els.play.onclick = () => Transport.toggle();
  els.resetSm.onclick = () => {                 // clear all solo/mute/volume → back to full mix
    S.rows.forEach(r => { r.solo = false; r.mute = false; r.vol = 1; });
    syncRowBtns(); Transport.refresh();
  };
  els.masterVol.oninput = () => Transport.setMaster(parseFloat(els.masterVol.value));
  els.masterVol.ondblclick = () => { els.masterVol.value = 1; Transport.setMaster(1); };
  els.lyricsBtn.onclick = () => Lyrics.toggle();
  els.simBtn.onclick = () => Similar.toggle();
  els.simClose.onclick = () => Similar.toggle();
  els.galBtn.onclick = () => Galaxy.toggle();
  els.galClose.onclick = () => Galaxy.toggle();
  els.radioBtn.onclick = () => Radio.toggle();
  els.tbarNext.onclick = () => Radio.advance();        // skip to the queued track now
  els.simRefresh.onclick = () => Similar.find();
  els.simFacet.onchange = () => { Similar.facet = els.simFacet.value; Similar.find(); };
  els.simQGo.onclick = () => Similar.textSearch();
  els.simQ.onkeydown = e => { if (e.key === "Enter") Similar.textSearch(); };
  { const _h = document.querySelector("header"); if (_h) els.simPanel.style.top = _h.offsetHeight + "px"; }
  els.toggleSub.onclick = () => {               // collapse/expand ALL groups at once
    const groups = groupsWithChildren();
    const allCollapsed = groups.every(g => S.collapsed.has(g));
    groups.forEach(g => setGroupCollapsed(g, !allCollapsed));
    updateCarets();
    applyVisibility();
  };
  els.fit.onclick = () => setView(0, S.meta.duration);
  els.lock.onclick = () => {
    S.lock = !S.lock;
    els.lock.classList.toggle("on", S.lock);
    if (S.lock) centerOnPlayhead();          // snap to center right away
  };
  document.querySelectorAll("#legend .lg").forEach(b => b.onclick = () => {
    S.show[b.dataset.k] = !S.show[b.dataset.k];
    b.classList.toggle("on", S.show[b.dataset.k]);
    drawOverlay();
  });
  els.zin.onclick = () => zoomBy(0.5);
  els.zout.onclick = () => zoomBy(2);
  els.compBtn.onclick = () => els.compPanel.hidden = !els.compPanel.hidden;
  document.addEventListener("click", e => {
    if (!e.target.closest(".comp-wrap")) els.compPanel.hidden = true;
  });

  window.addEventListener("resize", debounce(() => { applyVisibility(); Live.resize(); }, 120));
  setupInteractions();
  Transport.init();
  Ingest.setup();
  Live.setup();
  if (songs.length) loadSong(songs[0].id);
  else els.addPanel.hidden = false;     // no songs yet → invite an add
}

async function refreshSongs(selectId) {
  const songs = await fetch("/api/songs").then(r => r.json());
  const cur = els.song.value;
  els.song.innerHTML = songs.map(s => `<option value="${s.id}">${s.title}</option>`).join("");
  els.song.value = selectId || cur;
  if (selectId) loadSong(selectId);
}

async function loadSong(id) {
  S.songId = id;
  Radio._joinT = null;              // radio re-sets it right after when it drove the switch
  S.meta = await fetch("/api/song?id=" + encodeURIComponent(id)).then(r => r.json());
  document.title = `${id} — Microscope`;

  const f = S.meta.features || {};
  const keyName = (f.key != null && f.mode != null) ? keyLabel(f.key, f.mode) : "";
  const pb = S.meta.playback || {};
  let q = pb.full_quality
    ? `♪ ${pb.channels === 2 ? "stereo" : "mono"} ${(pb.sr / 1000).toFixed(1)}k`
    : `♪ 22k mono mix`;
  if (pb.lossy) q += " (lossy src)";
  if (pb.hq_vocals) q += " · ✨HQ vox";
  if (pb.drum_kit) q += " · 🥁kit";
  if (S.meta.tracks.some(t => t.id === "guitar" || t.id === "piano")) q += " · 🎸6-stem";
  S._info = [f.tempo ? Math.round(f.tempo) + " bpm" : "", keyName, q].filter(Boolean).join(" · ");

  S.visible = new Set(S.meta.tracks.map(t => t.id));   // all on by default
  applyCollapse();                                     // honor collapsed groups
  buildRows();
  buildCompPanel();
  Transport.reset();
  applyVisibility();
  setView(0, S.meta.duration);
  if (Lyrics.visible) Lyrics.load(id); else Lyrics.data = null;
}

// groups (parent stems) that actually have sub-components in this song
function groupsWithChildren() {
  const set = new Set();
  for (const t of (S.meta?.tracks || [])) if (t.depth === 1) set.add(t.group);
  return [...set];
}
function applyCollapse() {                              // remove hidden children from visible
  for (const t of S.meta.tracks) {
    if (t.depth === 1 && S.collapsed.has(t.group)) S.visible.delete(t.id);
  }
}
function setGroupCollapsed(g, collapsed) {
  collapsed ? S.collapsed.add(g) : S.collapsed.delete(g);
  for (const t of S.meta.tracks) {
    if (t.depth === 1 && t.group === g) collapsed ? S.visible.delete(t.id) : S.visible.add(t.id);
  }
}
function updateCarets() {
  for (const r of S.rows) {
    const cb = r.el.querySelector(".caret");
    if (cb) cb.textContent = S.collapsed.has(r.track.id) ? "▸" : "▾";
  }
  const groups = groupsWithChildren();
  const allCollapsed = groups.length && groups.every(g => S.collapsed.has(g));
  els.toggleSub.textContent = allCollapsed ? "▸ show sub" : "▾ hide sub";
  els.toggleSub.classList.toggle("on", allCollapsed);
  els.compPanel.querySelectorAll("input").forEach(cb => cb.checked = S.visible.has(cb.dataset.id));
}
function toggleGroup(g) {
  setGroupCollapsed(g, !S.collapsed.has(g));
  updateCarets();
  applyVisibility();
}
const keyLabel = (k, mode) =>
  ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][k] + (mode ? " maj" : " min");

// ==========================================================================
// rows + visibility + layout
// ==========================================================================
function buildRows() {
  els.tracks.innerHTML = "";
  S.rows = [];
  for (const t of S.meta.tracks) {
    const c = trackColor(t);
    const hasKids = t.depth === 0 && S.meta.tracks.some(x => x.depth === 1 && x.group === t.id);
    const caret = hasKids
      ? `<button class="caret" title="collapse/expand sub-components">${S.collapsed.has(t.id) ? "▸" : "▾"}</button>`
      : "";
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML =
      `<div class="label">
         <div class="nm ${t.depth ? "child" : ""}" style="color:${rgb(c)}">${caret}${t.label}</div>
         <div class="sm">
           <button class="solo" title="solo (hear only this)">S</button>
           <button class="mute" title="mute">M</button>
         </div>
         ${t.id === "mix" ? "" :
           `<input class="vol" type="range" min="0" max="1.5" step="0.01" value="1"
                   title="volume (double-click to reset)">`}
       </div>
       <div class="cv"><canvas></canvas></div>`;
    els.tracks.appendChild(row);
    const canvas = row.querySelector("canvas");
    const r = {
      track: t, el: row, canvas, ctx: canvas.getContext("2d"),
      color: c, w: 1, h: 1, cache: null, specCache: null, solo: false, mute: false, vol: 1,
    };
    row.querySelector(".solo").onclick = () => { r.solo = !r.solo; syncRowBtns(); Transport.refresh(); };
    row.querySelector(".mute").onclick = () => { r.mute = !r.mute; syncRowBtns(); Transport.refresh(); };
    const vol = row.querySelector(".vol");
    if (vol) {
      vol.oninput = () => Transport.setVol(r.track.id, parseFloat(vol.value));
      vol.ondblclick = () => { vol.value = 1; Transport.setVol(r.track.id, 1); };
    }
    const cb = row.querySelector(".caret");
    if (cb) cb.onclick = e => { e.stopPropagation(); toggleGroup(t.id); };
    S.rows.push(r);
  }
}
function syncRowBtns() {
  const dim = Transport.resolve ? Transport.resolve().dim : new Set();
  for (const r of S.rows) {
    r.el.querySelector(".solo").classList.toggle("on", r.solo);
    r.el.querySelector(".mute").classList.toggle("on", r.mute);
    r.el.classList.toggle("dim", dim.has(r.track.id));
    const v = r.el.querySelector(".vol");
    if (v && parseFloat(v.value) !== r.vol) v.value = r.vol;
    if (v) v.classList.toggle("adj", r.vol !== 1);
  }
}

function buildCompPanel() {
  const presets = [
    ["all", () => new Set(S.meta.tracks.map(t => t.id))],
    ["stems", () => new Set(["mix", ...STEMS])],
    ["drum kit", () => new Set(["drums", "kick", "snare", "hat"])],
    ["bass split", () => new Set(["bass", "sub", "mid", "high"])],
    ["vocals", () => new Set(["vocals"])],
  ];
  let html = `<div class="presets">` +
    presets.map((p, i) => `<button data-p="${i}">${p[0]}</button>`).join("") + `</div>`;
  let lastGroup = null;
  for (const t of S.meta.tracks) {
    if (t.group !== lastGroup && t.depth === 0) { html += `<div class="grp">`; lastGroup = t.group; }
    const c = trackColor(t);
    html += `<label class="${t.depth ? "child" : ""}">
        <input type="checkbox" data-id="${t.id}" ${S.visible.has(t.id) ? "checked" : ""}>
        <span class="sw" style="background:${rgb(c)}"></span>${t.label}</label>`;
  }
  els.compPanel.innerHTML = html;
  els.compPanel.querySelectorAll("input").forEach(cb => cb.onchange = () => {
    cb.checked ? S.visible.add(cb.dataset.id) : S.visible.delete(cb.dataset.id);
    applyVisibility();
  });
  els.compPanel.querySelectorAll(".presets button").forEach(b => b.onclick = () => {
    S.visible = presets[+b.dataset.p][1]();
    els.compPanel.querySelectorAll("input").forEach(cb =>
      cb.checked = S.visible.has(cb.dataset.id));
    applyVisibility();
  });
}

function applyVisibility() {
  const vis = S.rows.filter(r => S.visible.has(r.track.id));
  if (!vis.length) return;
  const avail = els.scroll.clientHeight;
  const rowH = clamp(Math.floor(avail / vis.length), 70, 520);
  for (const r of S.rows) {
    const on = S.visible.has(r.track.id);
    r.el.style.display = on ? "" : "none";
    if (on) r.el.style.height = rowH + "px";
  }
  layout();
  requestRender(true);
}

function layout() {
  for (const r of S.rows) {
    if (!S.visible.has(r.track.id)) continue;
    const rect = r.canvas.getBoundingClientRect();
    r.w = Math.max(1, Math.round(rect.width));
    r.h = Math.max(1, Math.round(rect.height));
    r.canvas.width = Math.round(r.w * DPR());
    r.canvas.height = Math.round(r.h * DPR());
  }
  sizeOverlay();
}
function sizeOverlay() {
  const gutter = gutterPx();
  const w = Math.max(1, els.tracks.clientWidth - gutter);
  const h = Math.max(1, els.tracks.offsetHeight);
  els.overlayWrap.style.height = h + "px";       // span all rows + scroll with them
  els.overlay.style.height = h + "px";
  els.overlay.width = Math.round(w * DPR());
  els.overlay.height = Math.round(h * DPR());
  const rw = Math.max(1, els.ruler.getBoundingClientRect().width);
  els.ruler.width = Math.round(rw * DPR());
  els.ruler.height = Math.round(26 * DPR());
}
const gutterPx = () =>
  parseInt(getComputedStyle(document.documentElement).getPropertyValue("--gutter"));

// ==========================================================================
// view + zoom/pan
// ==========================================================================
function setView(start, end) {
  const dur = S.meta.duration, minSpan = 80 / S.meta.sr;
  let span = clamp(end - start, minSpan, dur);
  start = clamp(start, 0, dur - span);
  S.view = { start, end: start + span };
  requestRender();
}
function centerOnPlayhead() {
  if (!S.meta) return;
  const t = Transport.curTime(), span = S.view.end - S.view.start;
  setView(t - span / 2, t + span / 2);
}
function zoomBy(factor, pivotT) {
  const span0 = S.view.end - S.view.start;
  const span = clamp(span0 * factor, 80 / S.meta.sr, S.meta.duration);
  if (pivotT == null) pivotT = (S.view.start + S.view.end) / 2;
  const frac = (pivotT - S.view.start) / span0;
  setView(pivotT - frac * span, pivotT - frac * span + span);
}
const timeToX = (t, w) => (t - S.view.start) / (S.view.end - S.view.start) * w;
const xToTime = (x, w) => S.view.start + (x / w) * (S.view.end - S.view.start);

// ==========================================================================
// render — instant cached redraw, debounced crisp refetch
// ==========================================================================
function requestRender(force) {
  drawRuler(); drawOverlay(); updateReadout();
  for (const r of S.rows) if (S.visible.has(r.track.id)) fastDraw(r);
  refetch(force);
}
const refetch = throttle(_refetch, 120);
function _refetch(force) {
  const id = ++S.renderId;
  for (const r of S.rows) if (S.visible.has(r.track.id)) fetchRow(r, id, force);
}

function updateReadout() {
  const span = S.view.end - S.view.start;
  const spp = span * S.meta.sr / (S.rows.find(r => S.visible.has(r.track.id))?.w || 1000);
  const z = spp < 1.5 ? `<b>sample-level</b> (${spp.toFixed(2)} samp/px)` : `${Math.round(spp)} samp/px`;
  els.readout.innerHTML =
    `${S._info ? S._info + " · " : ""}view <b>${fmtTime(S.view.start)}</b>–<b>${fmtTime(S.view.end)}</b> ` +
    `(${span < 1 ? (span * 1000).toFixed(0) + " ms" : span.toFixed(2) + " s"}) · ${z}`;
}

// -- waveform --------------------------------------------------------------
async function fetchRow(r, id, force) {
  if (S.mode === "spec" || S.mode === "both") fetchSpec(r, id);
  if (S.mode === "wave" || S.mode === "both") {
    const v = S.view;
    const url = `/api/peaks?id=${encodeURIComponent(S.songId)}&track=${r.track.id}` +
                `&start=${v.start}&end=${v.end}&width=${r.w}`;
    let d; try { d = await fetch(url).then(x => x.json()); } catch { return; }
    if (id !== S.renderId) return;
    r.cache = { data: d, start: v.start, end: v.end };
    fastDraw(r);
  } else {
    fastDraw(r);
  }
}
async function fetchSpec(r, id) {
  const v = S.view;
  const url = `/api/spectrogram?id=${encodeURIComponent(S.songId)}&track=${r.track.id}` +
              `&start=${v.start}&end=${v.end}&width=${r.w}&height=128`;
  const img = await new Promise(res => { const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = url; });
  if (id !== S.renderId || !img) return;
  r.specCache = { img, start: v.start, end: v.end };
  fastDraw(r);
}

function fastDraw(r) {
  const { ctx, w, h } = r;
  ctx.setTransform(DPR(), 0, 0, DPR(), 0, 0);
  ctx.clearRect(0, 0, w, h);
  if ((S.mode === "spec" || S.mode === "both") && r.specCache) drawSpec(r);
  if (S.mode === "wave" || S.mode === "both") drawWave(r);
}

function drawSpec(r) {
  const { ctx, w, h, specCache: sc } = r;
  const xL = timeToX(sc.start, w), xR = timeToX(sc.end, w);
  ctx.globalAlpha = S.mode === "both" ? 0.8 : 1;
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(sc.img, xL, 0, xR - xL, h);
  ctx.globalAlpha = 1;
}

function drawWave(r) {
  const { ctx, w, h, color } = r;
  const mid = h / 2, amp = h / 2 - 3, over = S.mode === "both";
  ctx.strokeStyle = over ? "rgba(255,255,255,.12)" : "rgba(255,255,255,.06)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();
  if (!r.cache) return;
  const { data: d, start, end } = r.cache;

  if (d.mode === "minmax") {
    const n = d.max.length; if (!n) return;
    const dt = (end - start) / n;
    ctx.beginPath();
    for (let i = 0; i < n; i++) ctx.lineTo(timeToX(start + i * dt, w), mid - d.max[i] * amp);
    for (let i = n - 1; i >= 0; i--) ctx.lineTo(timeToX(start + i * dt, w), mid - d.min[i] * amp);
    ctx.closePath();
    ctx.fillStyle = over ? rgb([255, 255, 255], 0.5) : rgb(color, 0.85);
    ctx.fill();
  } else if (d.mode === "samples") {
    const y = d.y, n = y.length; if (!n) return;
    const dxs = d.dt / (S.view.end - S.view.start) * w;
    ctx.strokeStyle = over ? rgb([255, 255, 255], 0.9) : rgb(color, 1);
    ctx.lineWidth = 1.4; ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const X = timeToX(d.t0 + i * d.dt, w), Y = mid - y[i] * amp;
      i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y);
    }
    ctx.stroke();
    if (dxs > 6) {
      ctx.fillStyle = over ? "#fff" : rgb(color, 1);
      for (let i = 0; i < n; i++) {
        ctx.beginPath(); ctx.arc(timeToX(d.t0 + i * d.dt, w), mid - y[i] * amp, 2, 0, 6.283); ctx.fill();
      }
    }
  }
}

// -- ruler + overlay -------------------------------------------------------
function drawRuler() {
  const c = els.ruler, ctx = c.getContext("2d"), dpr = DPR();
  const w = c.width / dpr, h = c.height / dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#6b7180"; ctx.font = "10px ui-monospace, monospace";
  const step = niceStep(S.view.end - S.view.start, w / 80);
  const first = Math.ceil(S.view.start / step) * step;
  ctx.strokeStyle = "rgba(255,255,255,.1)";
  for (let t = first; t <= S.view.end; t += step) {
    const x = timeToX(t, w);
    ctx.beginPath(); ctx.moveTo(x, h - 6); ctx.lineTo(x, h); ctx.stroke();
    ctx.fillText(fmtTime(t), x + 3, h - 8);
  }
}
function drawOverlay() {
  const c = els.overlay, ctx = c.getContext("2d"), dpr = DPR();
  const w = c.width / dpr, h = c.height / dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  const f = S.meta.features || {}, span = S.view.end - S.view.start;
  if (S.show.sections) {
    const secs = f.sections || [];
    for (let i = 0; i < secs.length; i++) {
      const x0 = timeToX(secs[i], w), x1 = i + 1 < secs.length ? timeToX(secs[i + 1], w) : w;
      if (x1 < 0 || x0 > w) continue;
      ctx.fillStyle = `hsla(${(i * 47) % 360},60%,55%,0.07)`; ctx.fillRect(x0, 0, x1 - x0, h);
      ctx.strokeStyle = "rgba(255,255,255,.18)";
      ctx.beginPath(); ctx.moveTo(x0, 0); ctx.lineTo(x0, h); ctx.stroke();
      if (f.section_labels?.[i]) {
        ctx.fillStyle = "rgba(255,255,255,.5)"; ctx.font = "11px ui-monospace, monospace";
        ctx.fillText(f.section_labels[i], x0 + 4, 13);
      }
    }
  }
  if (S.show.beats && (f.beats || []).length && span < 40) {
    ctx.strokeStyle = "rgba(255,255,255,.10)"; ctx.beginPath();
    for (const b of f.beats) {
      if (b < S.view.start || b > S.view.end) continue;
      const x = timeToX(b, w); ctx.moveTo(x, 0); ctx.lineTo(x, h);
    }
    ctx.stroke();
  }
  if (S.show.phrases) {
    for (const p of (f.phrases || [])) {
      if (p < S.view.start || p > S.view.end) continue;
      const x = timeToX(p, w);
      ctx.strokeStyle = "rgba(120,200,255,.35)"; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); ctx.setLineDash([]);
    }
  }
}
function niceStep(span, ticks) {
  const raw = span / Math.max(1, ticks), pow = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * pow >= raw) return m * pow;
  return 10 * pow;
}

// ==========================================================================
// interactions  (pinch = zoom, swipe/drag = pan, click = seek)
// ==========================================================================
function setupInteractions() {
  const area = els.scroll;
  const localX = clientX => clientX - area.getBoundingClientRect().left - gutterPx();
  const vw = () => S.rows.find(r => S.visible.has(r.track.id))?.w || area.clientWidth;

  area.addEventListener("wheel", e => {
    if (!S.meta) return;
    const w = vw();
    if (e.ctrlKey || e.metaKey) {                       // pinch / cmd+scroll = zoom
      e.preventDefault();
      zoomBy(Math.exp(clamp(e.deltaY, -40, 40) * 0.01), xToTime(clamp(localX(e.clientX), 0, w), w));
    } else if (e.shiftKey) {                            // shift+scroll = pan time
      e.preventDefault();
      const span = S.view.end - S.view.start;
      setView(S.view.start + e.deltaY / w * span, S.view.end + e.deltaY / w * span);
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {  // horizontal swipe = pan time
      e.preventDefault();
      const span = S.view.end - S.view.start;
      setView(S.view.start + e.deltaX / w * span, S.view.end + e.deltaX / w * span);
    }
    // else: vertical wheel → let the browser scroll the track list (no preventDefault)
  }, { passive: false });

  let dragging = false, lastX = 0, moved = 0;
  area.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".label")) return;
    dragging = true; moved = 0; lastX = e.clientX; area.setPointerCapture(e.pointerId);
  });
  area.addEventListener("pointermove", e => {
    const w = vw(), x = localX(e.clientX);
    if (x >= 0 && x <= w) { els.cursor.style.display = "block"; els.cursor.style.left = x + "px"; }
    else els.cursor.style.display = "none";
    if (!dragging) return;
    const dx = e.clientX - lastX; lastX = e.clientX; moved += Math.abs(dx);
    const span = S.view.end - S.view.start;
    setView(S.view.start - dx / w * span, S.view.end - dx / w * span);
  });
  area.addEventListener("pointerup", e => {
    if (dragging && moved < 4) {                        // treated as a click → seek
      const w = vw(), x = localX(e.clientX);
      if (x >= 0 && x <= w) Transport.seek(xToTime(x, w));
    }
    dragging = false;
  });
  area.addEventListener("pointerleave", () => { els.cursor.style.display = "none"; });

  window.addEventListener("keydown", e => {
    if (e.key === "Escape") {                 // close any open panel / overlay
      els.addPanel.hidden = true; els.compPanel.hidden = true;
      els.dropOverlay.hidden = true;
      if (Lyrics.visible) Lyrics.toggle();
      if (Galaxy.visible) Galaxy.toggle();
      if (e.target.blur) e.target.blur();
      return;
    }
    if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
    if (e.code === "Space") { e.preventDefault(); Transport.toggle(); }
    else if (e.key === "f") setView(0, S.meta.duration);
    else if (e.key === "=" || e.key === "+") zoomBy(0.5);
    else if (e.key === "-") zoomBy(2);
  });
}

// ==========================================================================
// Synced lyrics (karaoke) — fetches <id>_lyrics.json, highlights the current
// line + word against the transport clock, click a line/word to seek.
// ==========================================================================
// connector words that render small + light (everything else is "content")
const LS_STOP = new Set(("a an and the to of in on at is it i im you your we re they "
  + "he she his her my me for with as so but or nor if then that this these those be "
  + "been was were are am do did does got get up out off no not yeah oh ").split(" "));
const LS_STATE = ["ls-d0", "ls-d1", "ls-d2", "ls-d3", "ls-d4"];

// deterministic per-song hash + PRNG → stable editorial layout each load
function lsHash(s) { let h = 2166136261 >>> 0; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function lsRng(seed) { let s = seed >>> 0; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }
function lsTier(word, rng) {
  const n = word.toLowerCase().replace(/[^a-z']/g, "");
  if (!n || (LS_STOP.has(n) && n.length <= 4)) return "sm";
  if (n.length >= 7) return "lg";
  return rng() < 0.28 ? "lg" : "md";        // occasional emphasis bump
}
function lsTime(t) { t = Math.max(0, t | 0); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); }

const Lyrics = {
  data: null, lines: [], lineEls: [], curLine: -1, visible: false, _built: false,
  _spans: null, _words: null,

  _dom() {
    if (this._built) return;
    this.panel = els.lyricsPanel;
    this.stage = document.getElementById("ls-stage");
    this.flow = document.getElementById("ls-flow");
    this.bg = document.getElementById("ls-bg");
    this.elPlay = document.getElementById("ls-play");
    this.elTime = document.getElementById("ls-time");
    this.elDur = document.getElementById("ls-dur");
    this.scrub = document.getElementById("ls-scrub");
    this.scrubFill = document.getElementById("ls-scrub-fill");
    this.elTitle = document.getElementById("ls-title");
    this.elPlay.onclick = () => Transport.toggle();
    document.getElementById("ls-close").onclick = () => this.toggle();
    const seekAt = ev => {
      const r = this.scrub.getBoundingClientRect();
      if (S.meta) Transport.seek(clamp((ev.clientX - r.left) / r.width, 0, 1) * S.meta.duration);
    };
    this.scrub.onpointerdown = e => {
      seekAt(e); this.scrub.setPointerCapture(e.pointerId);
      this.scrub.onpointermove = ev => { if (ev.buttons) seekAt(ev); };
      this.scrub.onpointerup = () => { this.scrub.onpointermove = null; };
    };
    this._built = true;
  },

  async load(songId) {
    this._dom();
    this.data = null; this.lines = []; this.lineEls = []; this.curLine = -1;
    this.flow.classList.add("loading"); this.flow.innerHTML = "";
    this.elTitle.textContent = els.song.selectedOptions[0]?.textContent || "";
    this._applyTheme(songId);
    try {
      const r = await fetch("/api/lyrics?id=" + encodeURIComponent(songId));
      this.data = r.ok ? await r.json() : null;
    } catch { this.data = null; }
    this.render(songId);
  },

  _applyTheme(songId) {
    const hue = lsHash(songId) % 360, hue2 = (hue + 38) % 360;
    this.bg.style.background =
      `linear-gradient(155deg, hsl(${hue} 56% 21%), hsl(${hue2} 62% 9%) 68%, hsl(${hue} 46% 6%))`;
    this.panel.style.setProperty("--ls-hue", hue);
    this.panel.style.setProperty("--ls-accent", `hsl(${hue} 92% 74%)`);
  },

  render(songId) {
    this.lineEls = []; this.curLine = -1; this._spans = null;
    this.flow.classList.remove("loading"); this.flow.innerHTML = "";
    const lines = (this.data && this.data.lines) || [];
    this.lines = lines;
    if (!lines.length) { this.flow.innerHTML = `<div class="ls-nolyric">♪</div>`; return; }
    const rng = lsRng(lsHash(songId));
    for (const ln of lines) {
      const d = document.createElement("div");
      d.className = "ls-line"; d.style.setProperty("--indent", Math.floor(rng() * 15) + "%");
      const txt = (ln.text || "").trim();
      const words = ln.words && ln.words.length ? ln.words : null;
      if (words) {
        for (const w of words) {
          const s = document.createElement("span");
          s.className = "ls-word " + lsTier(w.w, rng); s.textContent = w.w;
          s.onclick = e => { e.stopPropagation(); if (S.meta) Transport.seek(w.t); };
          d.appendChild(s); d.appendChild(document.createTextNode(" "));
        }
      } else if (txt) {
        for (const tok of txt.split(/\s+/)) {
          const s = document.createElement("span");
          s.className = "ls-word " + lsTier(tok, rng); s.textContent = tok;
          d.appendChild(s); d.appendChild(document.createTextNode(" "));
        }
      } else { d.className = "ls-line ls-interlude"; d.textContent = "♪"; }
      if (ln.t != null) d.onclick = ev => { if (ev.target === d) Transport.seek(ln.t); };
      this.flow.appendChild(d); this.lineEls.push(d);
    }
    requestAnimationFrame(() => this._setActive(0, true));
  },

  // place the active line at the stage focal point (~42% down) via translateY
  _scrollTo(idx, instant) {
    const el = this.lineEls[idx]; if (!el) return;
    const y = this.stage.clientHeight * 0.42 - (el.offsetTop + el.offsetHeight / 2);
    if (instant) {
      this.flow.style.transition = "none"; this.flow.style.transform = `translateY(${y}px)`;
      void this.flow.offsetHeight; this.flow.style.transition = "";
    } else this.flow.style.transform = `translateY(${y}px)`;
  },

  _setActive(idx, instant) {
    if (this.curLine >= 0 && this.lineEls[this.curLine])     // clear old word highlight
      this.lineEls[this.curLine].querySelectorAll(".sung,.cur").forEach(s => s.classList.remove("sung", "cur"));
    for (let i = 0; i < this.lineEls.length; i++) {
      const el = this.lineEls[i];
      el.classList.remove(...LS_STATE);
      el.classList.add("ls-d" + Math.min(idx < 0 ? 4 : Math.abs(i - idx), 4));
    }
    this.curLine = idx;
    this._spans = idx >= 0 ? this.lineEls[idx].querySelectorAll(".ls-word") : null;
    this._words = idx >= 0 ? this.lines[idx].words : null;
    if (idx >= 0) this._scrollTo(idx, instant);
  },

  update(t) {
    if (!this.visible) return;
    if (this._built && S.meta) {                              // transport strip
      const dur = S.meta.duration || 0;
      this.scrubFill.style.width = (dur ? t / dur * 100 : 0) + "%";
      this.elTime.textContent = lsTime(t); this.elDur.textContent = lsTime(dur);
      this.elPlay.textContent = Transport.playing ? "❚❚" : "▶";
    }
    if (!this.lineEls.length) return;
    const lines = this.lines;
    let idx = -1;
    for (let i = 0; i < lines.length; i++) { if (lines[i].t == null) continue; if (lines[i].t <= t) idx = i; else break; }
    if (idx !== this.curLine) this._setActive(idx, false);
    if (this._spans && this._words && this._spans.length === this._words.length)
      for (let j = 0; j < this._words.length; j++) {
        const w = this._words[j];
        this._spans[j].classList.toggle("sung", t >= w.t);
        this._spans[j].classList.toggle("cur", t >= w.t && t < (w.end ?? w.t + 0.4));
      }
  },

  toggle() {
    this.visible = !this.visible;
    document.body.classList.toggle("lyrics-on", this.visible);
    els.lyricsBtn.classList.toggle("on", this.visible);
    if (this.visible) {
      if (!this.data && S.songId) this.load(S.songId);
      else if (this.lineEls.length) requestAnimationFrame(() => this._scrollTo(Math.max(0, this.curLine), true));
    }
  },
};

// ==========================================================================
// Persistent transport / seek bar (bottom of #app). Stays visible in every view,
// so you can scrub the song while in scene or live mode. Driven by Transport.tick.
// ==========================================================================
const TBar = {
  _built: false,
  _dom() {
    if (this._built) return;
    this.play = $("#tbar-play"); this.cur = $("#tbar-cur"); this.dur = $("#tbar-dur");
    this.track = $("#tbar-track"); this.fill = $("#tbar-fill");
    if (!this.track) return;                         // markup missing → retry next frame
    this.play.onclick = () => Transport.toggle();
    const seekAt = ev => {
      const r = this.track.getBoundingClientRect();
      if (S.meta) Transport.seek(clamp((ev.clientX - r.left) / r.width, 0, 1) * S.meta.duration);
    };
    this.track.onpointerdown = e => {
      this.track.setPointerCapture(e.pointerId); seekAt(e);
      this.track.onpointermove = ev => { if (ev.buttons) seekAt(ev); };
      this.track.onpointerup = () => { this.track.onpointermove = null; };
    };
    this._built = true;
  },
  update(t) {
    this._dom();
    if (!this._built || !S.meta) return;
    const dur = S.meta.duration || 0;
    this.fill.style.width = (dur ? t / dur * 100 : 0) + "%";
    this.cur.textContent = lsTime(t);
    this.dur.textContent = lsTime(dur);
    this.play.textContent = Transport.playing ? "❚❚" : "▶";
    // radio transition markers: where we joined, where we'll leave
    const exitT = dur - Radio.FADE - 0.5;
    els.tbarExit.hidden = !(Radio.on && dur > 0 && exitT > 0);
    if (!els.tbarExit.hidden) els.tbarExit.style.left = (exitT / dur * 100) + "%";
    els.tbarJoin.hidden = !(Radio.on && dur > 0 && Radio._joinT != null);
    if (!els.tbarJoin.hidden) els.tbarJoin.style.left = (Radio._joinT / dur * 100) + "%";
  },
};

// ==========================================================================
// Moment similarity explorer — "find moments across the library that sound like
// the current spot". Queries /api/similar (MERT moment kNN) and lets you jump to
// any result, which loads that song and plays from the matching moment.
// ==========================================================================
const Similar = {
  visible: false, facet: "mix", _poll: null,
  _prev: null, _prevBtn: null, _prevCard: null,   // inline preview player state

  toggle() {
    this.visible = !this.visible;
    document.body.classList.toggle("sim-on", this.visible);
    els.simBtn.classList.toggle("on", this.visible);
    clearTimeout(this._poll);
    if (this.visible) this.find();
    else this.stopPreview();
  },

  async find() {
    if (!this.visible) return;
    if (!S.songId || !S.meta) { els.simResults.innerHTML = `<div class="sim-empty">load a song first</div>`; return; }
    const t = Transport.curTime();
    els.simSeed.innerHTML = `matching <b>${escapeHtml(els.song.selectedOptions[0]?.textContent || S.songId)}</b> @ ${lsTime(t)}`;
    els.simResults.innerHTML = `<div class="sim-empty">searching…</div>`;
    const url = `/api/similar?id=${encodeURIComponent(S.songId)}&t=${t.toFixed(2)}&facet=${this.facet}&k=16`;
    let r;
    try { r = await fetch(url); } catch { els.simResults.innerHTML = `<div class="sim-empty">request failed</div>`; return; }
    if (r.status === 202) {                       // index still building → poll
      const s = await r.json().catch(() => ({}));
      const pct = s.total ? Math.round((s.loaded / s.total) * 100) : 0;
      els.simResults.innerHTML = `<div class="sim-empty">building similarity index… ${pct}% (${s.loaded || 0}/${s.total || 0})</div>`;
      clearTimeout(this._poll);
      this._poll = setTimeout(() => this.find(), 1500);
      return;
    }
    const d = await r.json().catch(() => null);
    if (!d || d.error) { els.simResults.innerHTML = `<div class="sim-empty">${d ? escapeHtml(d.error) : "error"}</div>`; return; }
    this.render(d);
  },

  render(d) {
    els.simResults.innerHTML = "";
    const rs = d.results || [];
    if (!rs.length) { els.simResults.innerHTML = `<div class="sim-empty">no matches</div>`; return; }
    for (const res of rs) {
      const card = document.createElement("div"); card.className = "sim-card";
      const bar = document.createElement("div"); bar.className = "sim-bar";
      bar.style.width = Math.max(6, res.score * 100).toFixed(0) + "%";
      const pv = document.createElement("button"); pv.className = "sim-play"; pv.textContent = "▶";
      pv.title = "preview this moment";
      const info = document.createElement("div"); info.className = "sim-info";
      const title = document.createElement("span"); title.className = "sim-title"; title.textContent = res.title;
      const meta = document.createElement("span"); meta.className = "sim-time";
      meta.textContent = `${lsTime(res.start_t)}  ·  ${res.score.toFixed(2)}`;
      const open = document.createElement("button"); open.className = "sim-open"; open.textContent = "↗";
      open.title = "open this song here";
      info.append(title, meta); card.append(bar, pv, info, open);
      // card + ▶ both preview inline; only the small ↗ switches the whole app
      pv.onclick = ev => { ev.stopPropagation(); this.preview(res, pv, card); };
      open.onclick = ev => { ev.stopPropagation(); this.jump(res.song_id, res.start_t); };
      card.onclick = () => this.preview(res, pv, card);
      els.simResults.appendChild(card);
    }
  },

  // text -> moment search over the whole library (CLAP embeddings)
  async textSearch() {
    const qt = els.simQ.value.trim();
    if (!qt) return;
    clearTimeout(this._poll);
    els.simSeed.innerHTML = `searching for <b>“${escapeHtml(qt)}”</b>`;
    els.simResults.innerHTML = `<div class="sim-empty">searching…</div>`;
    let r;
    try { r = await fetch(`/api/textsearch?q=${encodeURIComponent(qt)}&k=24`); }
    catch { els.simResults.innerHTML = `<div class="sim-empty">request failed</div>`; return; }
    if (r.status === 202) {                       // audio index still loading
      els.simResults.innerHTML = `<div class="sim-empty">building sound index…</div>`;
      this._poll = setTimeout(() => this.textSearch(), 2000);
      return;
    }
    const d = await r.json().catch(() => null);
    if (!d || d.error) { els.simResults.innerHTML = `<div class="sim-empty">${d ? escapeHtml(d.error) : "error"}</div>`; return; }
    const cov = d.coverage || {};
    els.simSeed.innerHTML = `<b>“${escapeHtml(qt)}”</b> · searched ${cov.songs || 0}/${cov.total || "?"} songs`;
    this.render(d);
  },

  // play just this moment through a lightweight side-channel <audio>, without
  // touching the main transport (beyond pausing it so they don't overlap)
  preview(res, btn, card) {
    if (this._prevBtn === btn) { this.stopPreview(); return; }   // toggle off
    this.stopPreview();
    if (Transport.playing) Transport.toggle();
    const a = this._prev || (this._prev = new Audio());
    a.src = `/api/clip?id=${encodeURIComponent(res.song_id)}` +
            `&start=${res.start_t.toFixed(2)}&end=${(res.end_t + 2).toFixed(2)}`;
    a.volume = Math.min(1, Transport.masterVol);
    a.onended = () => this.stopPreview();
    a.play().catch(() => this.stopPreview());
    btn.textContent = "■"; card.classList.add("previewing");
    this._prevBtn = btn; this._prevCard = card;
  },

  stopPreview() {
    if (this._prev) { this._prev.pause(); this._prev.removeAttribute("src"); }
    if (this._prevBtn) this._prevBtn.textContent = "▶";
    if (this._prevCard) this._prevCard.classList.remove("previewing");
    this._prevBtn = this._prevCard = null;
  },

  async jump(songId, t) {
    this.stopPreview();
    if (S.songId !== songId) {
      els.song.value = songId;
      await loadSong(songId);
    }
    Transport.seek(t);
    if (!Transport.playing) await Transport.toggle();
    if (this.visible) this.find();                // chain discovery from the new spot
  },
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ==========================================================================
// Galaxy map — every moment of every song as a WebGL starfield (2-D UMAP of
// the MERT embeddings, computed offline by compute_galaxy.py). Hover a star
// to hear that moment; click to open the song there.
// ==========================================================================
const Galaxy = {
  visible: false, _loaded: false, _loading: false,
  n: 0, ids: [], xy: null, t: null, dur: null, sidx: null,
  gl: null, _progs: null, _grid: null, _cell: 0.02,
  view: { cx: 0, cy: 0, zoom: 0.9 },
  _hover: -1, _prev: null, _prevIdx: -1, _dwell: null,
  _ranges: null,                     // song_idx -> [first, count] (points are stored per-song contiguous)

  toggle() {
    this.visible = !this.visible;
    els.galaxy.hidden = !this.visible;
    els.galBtn.classList.toggle("on", this.visible);
    if (this.visible) { this.load(); this.resize(); }
    else this.stopPreview();
  },

  async load() {
    if (this._loaded || this._loading) return;
    this._loading = true;
    try {
      const meta = await fetch("/api/galaxy_meta").then(r => r.json());
      if (meta.error) { els.galStatus.textContent = meta.error; return; }
      const buf = await fetch("/api/galaxy_points").then(r => r.arrayBuffer());
      const n = this.n = meta.n;
      this.ids = meta.song_ids;
      this.xy = new Float32Array(buf, 0, 2 * n);
      this.t = new Float32Array(buf, 8 * n, n);
      this.dur = new Float32Array(buf, 12 * n, n);
      this.sidx = new Uint32Array(buf, 16 * n, n);
      // per-song contiguous ranges (for highlighting the loaded song)
      this._ranges = new Map();
      for (let i = 0; i < n; i++) {
        const s = this.sidx[i], r = this._ranges.get(s);
        if (r) r[1]++; else this._ranges.set(s, [i, 1]);
      }
      // spatial hash for hover lookup
      this._grid = new Map();
      const cs = this._cell;
      for (let i = 0; i < n; i++) {
        const k = `${Math.floor(this.xy[2 * i] / cs)},${Math.floor(this.xy[2 * i + 1] / cs)}`;
        const cell = this._grid.get(k);
        if (cell) cell.push(i); else this._grid.set(k, [i]);
      }
      this.initGL(new Uint8Array(buf, 20 * n, 3 * n));
      this._loaded = true;
      els.galStatus.textContent = `${n.toLocaleString()} moments · ${this.ids.length} songs`;
      this.render();
    } catch (e) {
      els.galStatus.textContent = "map failed to load";
    } finally { this._loading = false; }
  },

  initGL(rgb) {
    const gl = this.gl = els.galCanvas.getContext("webgl", { antialias: false, alpha: false });
    const VS = `attribute vec2 a_pos; attribute vec3 a_col;
      uniform vec2 u_off; uniform vec2 u_scale; uniform float u_size;
      varying vec3 v_col;
      void main() {
        gl_Position = vec4((a_pos - u_off) * u_scale, 0., 1.);
        gl_PointSize = u_size; v_col = a_col;
      }`;
    const FS = `precision mediump float; varying vec3 v_col; uniform float u_boost;
      void main() {
        float d = length(gl_PointCoord - .5);
        if (d > .5) discard;
        float a = smoothstep(.5, .12, d);
        vec3 c = mix(v_col, vec3(1.), u_boost);
        gl_FragColor = vec4(c * a, a);
      }`;
    const sh = (type, src) => {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VS));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(p);
    gl.useProgram(p);
    this._progs = {
      p,
      a_pos: gl.getAttribLocation(p, "a_pos"), a_col: gl.getAttribLocation(p, "a_col"),
      u_off: gl.getUniformLocation(p, "u_off"), u_scale: gl.getUniformLocation(p, "u_scale"),
      u_size: gl.getUniformLocation(p, "u_size"), u_boost: gl.getUniformLocation(p, "u_boost"),
    };
    const posBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, this.xy, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(this._progs.a_pos);
    gl.vertexAttribPointer(this._progs.a_pos, 2, gl.FLOAT, false, 0, 0);
    const colBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, colBuf);
    gl.bufferData(gl.ARRAY_BUFFER, rgb, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(this._progs.a_col);
    gl.vertexAttribPointer(this._progs.a_col, 3, gl.UNSIGNED_BYTE, true, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);          // additive: overlapping stars glow
    this.bindEvents();
  },

  resize() {
    const c = els.galCanvas, d = DPR();
    c.width = c.clientWidth * d; c.height = c.clientHeight * d;
    if (this.gl) { this.gl.viewport(0, 0, c.width, c.height); this.render(); }
  },

  // world <-> screen. zoom=1 fits world [-1,1] vertically.
  _scale() {
    const c = els.galCanvas, aspect = c.clientWidth / Math.max(1, c.clientHeight);
    return [this.view.zoom / aspect, this.view.zoom];
  },
  toWorld(px, py) {
    const c = els.galCanvas, [sx, sy] = this._scale();
    return [(2 * px / c.clientWidth - 1) / sx + this.view.cx,
            (1 - 2 * py / c.clientHeight) / sy + this.view.cy];
  },

  render() {
    const gl = this.gl;
    if (!gl || !this.visible) return;
    const P = this._progs, [sx, sy] = this._scale();
    gl.clearColor(0.02, 0.02, 0.045, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform2f(P.u_off, this.view.cx, this.view.cy);
    gl.uniform2f(P.u_scale, sx, sy);
    const base = clamp(1.6 * Math.sqrt(this.view.zoom) * DPR(), 1.5, 14);
    gl.uniform1f(P.u_size, base);
    gl.uniform1f(P.u_boost, 0);
    gl.drawArrays(gl.POINTS, 0, this.n);
    // highlight the loaded song's constellation
    const cur = this.ids.indexOf(S.songId);
    const r = cur >= 0 && this._ranges.get(cur);
    if (r) {
      gl.uniform1f(P.u_size, base * 2.2);
      gl.uniform1f(P.u_boost, 0.55);
      gl.drawArrays(gl.POINTS, r[0], r[1]);
    }
    if (this._hover >= 0) {
      gl.uniform1f(P.u_size, base * 3.2);
      gl.uniform1f(P.u_boost, 0.85);
      gl.drawArrays(gl.POINTS, this._hover, 1);
    }
  },

  nearest(wx, wy, maxWorld) {
    const cs = this._cell, gx = Math.floor(wx / cs), gy = Math.floor(wy / cs);
    const reach = Math.max(1, Math.ceil(maxWorld / cs));
    let best = -1, bd = maxWorld * maxWorld;
    for (let dx = -reach; dx <= reach; dx++)
      for (let dy = -reach; dy <= reach; dy++) {
        const cell = this._grid.get(`${gx + dx},${gy + dy}`);
        if (!cell) continue;
        for (const i of cell) {
          const ddx = this.xy[2 * i] - wx, ddy = this.xy[2 * i + 1] - wy;
          const d = ddx * ddx + ddy * ddy;
          if (d < bd) { bd = d; best = i; }
        }
      }
    return best;
  },

  bindEvents() {
    const c = els.galCanvas;
    let dragging = false, moved = false, lx = 0, ly = 0;
    c.onpointerdown = ev => { dragging = true; moved = false; lx = ev.clientX; ly = ev.clientY; c.setPointerCapture(ev.pointerId); };
    c.onpointerup = ev => {
      dragging = false; c.releasePointerCapture(ev.pointerId);
      if (!moved && this._hover >= 0) this.open(this._hover);
    };
    c.onpointermove = ev => {
      if (dragging) {
        const dx = ev.clientX - lx, dy = ev.clientY - ly;
        if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
        const [sx, sy] = this._scale();
        this.view.cx -= (2 * dx / c.clientWidth) / sx;
        this.view.cy += (2 * dy / c.clientHeight) / sy;
        lx = ev.clientX; ly = ev.clientY;
        this.render();
        return;
      }
      const rect = c.getBoundingClientRect();
      const [wx, wy] = this.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
      const [, sy] = this._scale();
      const i = this.nearest(wx, wy, (2 * 14 / c.clientHeight) / sy);   // ~14 px radius
      if (i !== this._hover) {
        this._hover = i;
        this.render();
        this.updateTip(i, ev.clientX, ev.clientY);
        clearTimeout(this._dwell);
        if (i >= 0) this._dwell = setTimeout(() => this.preview(i), 260);
        else this.stopPreview();
      } else if (i >= 0) {
        els.galTip.style.left = (ev.clientX + 14) + "px";
        els.galTip.style.top = (ev.clientY + 10) + "px";
      }
    };
    c.onpointerleave = () => {
      this._hover = -1; els.galTip.hidden = true;
      clearTimeout(this._dwell); this.stopPreview(); this.render();
    };
    c.onwheel = ev => {
      ev.preventDefault();
      const rect = c.getBoundingClientRect();
      const [wx, wy] = this.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
      const f = Math.pow(1.0015, -ev.deltaY);
      this.view.zoom = clamp(this.view.zoom * f, 0.3, 400);
      const [wx2, wy2] = this.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
      this.view.cx += wx - wx2; this.view.cy += wy - wy2;   // zoom toward cursor
      this.render();
    };
    window.addEventListener("resize", () => this.visible && this.resize());
  },

  updateTip(i, cx, cy) {
    if (i < 0) { els.galTip.hidden = true; return; }
    els.galTip.innerHTML = `<b>${escapeHtml(this.ids[this.sidx[i]])}</b><span>@ ${lsTime(this.t[i])}</span>`;
    els.galTip.style.left = (cx + 14) + "px";
    els.galTip.style.top = (cy + 10) + "px";
    els.galTip.hidden = false;
  },

  preview(i) {
    if (this._prevIdx === i) return;
    this.stopPreview();
    if (Transport.playing) Transport.toggle();
    const sid = this.ids[this.sidx[i]];
    const s = Math.max(0, this.t[i] - this.dur[i] / 2), e = this.t[i] + this.dur[i] / 2 + 1.5;
    const a = this._prev || (this._prev = new Audio());
    a.src = `/api/clip?id=${encodeURIComponent(sid)}&start=${s.toFixed(2)}&end=${e.toFixed(2)}`;
    a.volume = Math.min(1, Transport.masterVol);
    a.play().catch(() => {});
    this._prevIdx = i;
  },

  stopPreview() {
    clearTimeout(this._dwell);
    if (this._prev) { this._prev.pause(); this._prev.removeAttribute("src"); }
    this._prevIdx = -1;
  },

  async open(i) {
    const sid = this.ids[this.sidx[i]], t = Math.max(0, this.t[i] - this.dur[i] / 2);
    this.stopPreview();
    this.toggle();                                 // close the map
    if (S.songId !== sid) {
      els.song.value = sid;
      await loadSong(sid);
    }
    Transport.seek(t);
    if (!Transport.playing) await Transport.toggle();
  },
};

// ==========================================================================
// Infinite radio — when a song ends, walk the similarity graph into the next
// one: the outro's closest moment elsewhere in the library becomes the entry
// point of the next track. Facet follows the ✧ similar panel's selector.
// ==========================================================================
const Radio = {
  on: false, _next: null, _picking: false, _advancing: false, history: [],
  FADE: 5, FADEIN: 3,                    // seconds: outro fade-out / entry fade-in
  _fading: false, _fadeT: null, _fadeInPending: false,
  _joinT: null, _nextBuf: null, _nextBufFor: null,

  toggle() {
    this.on = !this.on;
    els.radioBtn.classList.toggle("on", this.on);
    els.tbarNext.hidden = !this.on;
    this._next = null;
    this.showNext();
    if (!this.on) this.cancelFade();
    if (this.on && S.songId) this.pick();
  },

  // start easing the outro down; when it bottoms out, flow into the next track.
  // (waiting for the literal last sample sounds abrupt — outros often just die.)
  beginExit() {
    if (this._fading || this._advancing || !Transport.playing) return;
    this._fading = true;
    const ac = Transport.ac, g = Transport.master.gain;
    g.cancelScheduledValues(ac.currentTime);
    g.setValueAtTime(Math.max(0.0001, g.value), ac.currentTime);
    g.exponentialRampToValueAtTime(0.0001, ac.currentTime + this.FADE);
    // hand off at ~70% of the fade: the exponential ramp is ≈ -56 dB there, so
    // cutting the tail is inaudible and the next track starts with no dead air.
    this._fadeT = setTimeout(() => {
      if (Transport.playing) this.advance(); else this.cancelFade();
    }, this.FADE * 700);
  },

  cancelFade() {
    clearTimeout(this._fadeT);
    this._fadeInPending = false;
    if (this._fading) {
      const ac = Transport.ac, g = Transport.master.gain;
      g.cancelScheduledValues(ac.currentTime);
      g.setValueAtTime(Math.max(0.0001, Transport.masterVol), ac.currentTime);
      this._fading = false;
    }
  },

  // called by Transport.startSources at the instant new audio starts: if an
  // entry fade is queued, ramp up from silence instead of snapping to full.
  consumeFadeIn() {
    if (!this._fadeInPending) return false;
    this._fadeInPending = false;
    const ac = Transport.ac, g = Transport.master.gain;
    g.cancelScheduledValues(ac.currentTime);
    g.setValueAtTime(0.0001, ac.currentTime);
    g.linearRampToValueAtTime(Math.max(0.0001, Transport.masterVol), ac.currentTime + this.FADEIN);
    return true;
  },

  async pick() {
    if (this._picking || !S.meta || !S.songId) return;
    this._picking = true;
    try {
      const t = Math.max(0, S.meta.duration - 15);       // seed from the outro
      let cand = null;
      try {
        const r = await fetch(`/api/similar?id=${encodeURIComponent(S.songId)}` +
                              `&t=${t.toFixed(2)}&facet=${Similar.facet}&k=12`);
        if (r.ok) {
          const d = await r.json();
          const seen = new Set([S.songId, ...this.history]);
          const fresh = (d.results || []).filter(x => !seen.has(x.song_id));
          // prefer joining somewhere that leaves plenty of song left to play
          cand = fresh.find(x => (x.start_t || 0) <= 180) || fresh[0] || null;
        }
      } catch {}
      if (!cand) {                                       // index building / unknown song
        const opts = [...els.song.options].map(o => o.value)
          .filter(v => v !== S.songId && !this.history.includes(v));
        const id = opts[Math.floor(Math.random() * opts.length)];
        if (id) cand = { song_id: id, start_t: 0, score: 0 };
      }
      this._next = cand;
      this.showNext();
      if (cand) this.predecode(cand.song_id);   // decode now → gapless switch later
    } finally { this._picking = false; }
  },

  // fetch + decode the next song's full mix during the outro, so the switch
  // costs ~nothing (the buffer is handed straight to the transport).
  async predecode(sid) {
    this._nextBuf = null; this._nextBufFor = sid;
    try {
      const ab = await fetch(`/api/audio?id=${encodeURIComponent(sid)}`).then(r => r.arrayBuffer());
      const buf = await Transport.ac.decodeAudioData(ab);
      if (this._nextBufFor === sid) this._nextBuf = buf;   // still the queued track
    } catch {}
  },

  showNext() {
    els.tbarNextLabel.textContent = this._next ? `${this._next.song_id}` : "…";
    els.tbarNext.title = this._next
      ? `up next: ${this._next.song_id} (joins at ${lsTime(this._next.start_t || 0)}) — click to skip there now`
      : "picking the next track…";
  },

  async advance() {
    if (this._advancing) return;
    this._advancing = true;
    clearTimeout(this._fadeT);
    try {
      if (!this._next) await this.pick();
      const nx = this._next;
      this._next = null;
      if (!nx) { Transport.pause(); return; }
      this.history.push(S.songId);
      if (this.history.length > 12) this.history.shift();
      this._fadeInPending = true;                        // ease the entry in
      els.song.value = nx.song_id;
      await loadSong(nx.song_id);
      if (this._nextBuf && this._nextBufFor === nx.song_id)
        Transport.buffers.set("full", this._nextBuf);    // pre-decoded: no decode gap
      this._nextBuf = null; this._nextBufFor = null;
      const joinT = Math.max(0, (nx.start_t || 0) - 2);
      Transport.seek(joinT);
      this._joinT = joinT;                               // ⟟ marker in the seek bar
      if (!Transport.playing) await Transport.toggle();
      this.showNext();
      this.pick();                                       // queue the following one
    } finally { this._advancing = false; this._fading = false; }
  },
};

// ==========================================================================
// WebAudio transport with solo / mute over decomposed components
// ==========================================================================
const Transport = {
  ac: null, master: null, buffers: new Map(), loading: new Map(),
  sources: [], playing: false, offset: 0, startedAt: 0, _starting: false, activeKey: "",
  analysers: {}, sceneMode: false, _td: null, _fd: null,
  gainNodes: {}, _mode: null,           // per-stem GainNodes + "full"|"components"
  masterVol: 1,                         // overall tab volume (header dial)

  setMaster(v) { this.masterVol = v; if (this.master) this.master.gain.value = v; },
  rowVol(id) { const r = S.rows.find(x => x.track.id === id); return r ? (r.vol ?? 1) : 1; },
  // any non-mix fader moved off unity → we must remix from individual stems
  anyVolAdjusted() { return S.rows.some(r => r.track.id !== "mix" && (r.vol ?? 1) !== 1); },

  init() {
    this.ac = new (window.AudioContext || window.webkitAudioContext)();
    this.master = this.ac.createGain();
    // safety limiter: hot stem sums / boosted faders squash instead of clipping
    this.limiter = this.ac.createDynamicsCompressor();
    this.limiter.threshold.value = -3; this.limiter.knee.value = 0;
    this.limiter.ratio.value = 20; this.limiter.attack.value = 0.002;
    this.limiter.release.value = 0.15;
    this.master.connect(this.limiter); this.limiter.connect(this.ac.destination);
    requestAnimationFrame(() => this.tick());
  },
  reset() {
    this.stopSources(); this.playing = false; this.offset = 0;
    this.buffers.clear(); this.loading.clear();
    els.play.textContent = "▶ play"; els.play.classList.remove("on");
  },

  // Hierarchical solo/mute over the stem tree. Returns the leaf buffers to
  // play AND the set of rows to dim. Mute a parent → whole group silent (kids
  // dimmed); mute a child → that slice subtracted from its group; solo isolates.
  resolve() {
    const rows = S.rows, byId = id => rows.find(r => r.track.id === id);
    const anyMute = rows.some(r => r.mute), anySolo = rows.some(r => r.solo);
    const dim = new Set();
    if (!anyMute && !anySolo) {
      if (!this.anyVolAdjusted()) return { play: ["full"], dim };      // untouched → original
      // faders moved but nothing soloed/muted → remix the base stems so each
      // can be scaled independently (the "full" buffer is pre-mixed).
      return { play: rows.filter(r => r.track.depth === 0 && r.track.id !== "mix")
                          .map(r => r.track.id), dim };
    }
    const mixRow = byId("mix");
    if (mixRow && mixRow.solo) {                                       // solo mix = whole song
      rows.forEach(r => { if (r.track.id !== "mix") dim.add(r.track.id); });
      return { play: ["full"], dim };
    }
    const groups = rows.filter(r => r.track.depth === 0 && r.track.id !== "mix").map(r => r.track.id);
    const kidsOf = g => rows.filter(r => r.track.group === g && r.track.depth === 1).map(r => r.track.id);
    const soloExists = rows.some(r => r.solo && r.track.id !== "mix");

    const play = [];
    for (const g of groups) {
      const kids = kidsOf(g);
      const kidSolo = kids.filter(k => byId(k).solo);
      const kidMute = kids.filter(k => byId(k).mute);
      const groupActive = !soloExists || byId(g).solo || kidSolo.length > 0;
      if (byId(g).mute || !groupActive) {                             // whole group silent
        dim.add(g); kids.forEach(k => dim.add(k));
        continue;
      }
      if (kidSolo.length) {                                           // isolate soloed kids
        play.push(...kidSolo);
        kids.forEach(k => { if (!kidSolo.includes(k)) dim.add(k); });
      } else if (kidMute.length) {                                    // subtract muted kids
        play.push(...kids.filter(k => !byId(k).mute));
        kidMute.forEach(k => dim.add(k));
      } else {
        play.push(g);                                                 // whole stem
      }
    }
    return { play, dim };
  },
  quality() {
    const ids = this.resolve().play;
    if (ids.length === 1 && ids[0] === "full") return "full";
    if (!ids.length) return "silent";
    return "components";
  },

  url(id) { return `/api/audio?id=${encodeURIComponent(S.songId)}&track=${id}`; },
  async load(id) {
    if (this.buffers.has(id)) return this.buffers.get(id);
    if (this.loading.has(id)) return this.loading.get(id);
    const p = fetch(this.url(id)).then(r => r.arrayBuffer())
      .then(ab => this.ac.decodeAudioData(ab))
      .then(buf => { this.buffers.set(id, buf); this.loading.delete(id); return buf; });
    this.loading.set(id, p); return p;
  },

  curTime() {
    // While (re)starting sources we await buffer decode; freeze at `offset` so the
    // playhead/lyrics don't run ahead of audio that hasn't actually started yet.
    const t = (this.playing && !this._starting)
      ? this.offset + (this.ac.currentTime - this.startedAt) : this.offset;
    return clamp(t, 0, S.meta ? S.meta.duration : 0);
  },
  stopSources() { for (const s of this.sources) { try { s.stop(); } catch {} } this.sources = []; },

  async startSources() {
    // generation token: rapid re-entries (e.g. a fader drag crossing the
    // full<->components boundary fires many refreshes while stems are still
    // decoding) must NOT each start their own set of sources — last call wins.
    const gen = this._gen = (this._gen || 0) + 1;
    this.stopSources();
    this.analysers = {};
    this.gainNodes = {};
    this._starting = true;              // freeze the clock until audio truly starts
    const off = this.offset;

    if (this.sceneMode && typeof Scene !== "undefined" && Scene.allInstrumentIds) {
      // Audio = full-quality original (unless soloing/muting); per-instrument
      // stems play SILENTLY into analysers so the visuals stay reactive.
      const instIds = Scene.allInstrumentIds();
      const aud = Scene.audibleSet();              // "FULL" or Set of audible ids
      const playFull = aud === "FULL";
      const loadIds = playFull ? ["full", ...instIds] : instIds;
      const key = "scene:" + loadIds.join(",");
      this.activeKey = key;
      const bufs = await Promise.all(loadIds.map(id => this.load(id)));
      if (gen !== this._gen || !this.playing) return;   // superseded while decoding
      this.startedAt = this.ac.currentTime; this._starting = false;   // anchor clock to real audio start
      if (!(typeof Radio !== "undefined" && Radio.consumeFadeIn()))
        this.master.gain.value = this.masterVol;
      let k = 0;
      if (playFull) {
        const buf = bufs[k++];
        if (buf) { const s = this.ac.createBufferSource(); s.buffer = buf; s.connect(this.master); s.start(0, Math.min(off, buf.duration)); this.sources.push(s); }
      }
      for (const id of instIds) {
        const buf = bufs[k++]; if (!buf) continue;
        const src = this.ac.createBufferSource(); src.buffer = buf;
        const an = this.ac.createAnalyser(); an.fftSize = 2048; an.smoothingTimeConstant = 0.6;
        const g = this.ac.createGain();
        g.gain.value = ((!playFull && aud.has(id)) ? 1 : 0) * this.rowVol(id);
        src.connect(an); an.connect(g); g.connect(this.master);
        this.analysers[id] = an; this.gainNodes[id] = g;
        src.start(0, Math.min(off, buf.duration)); this.sources.push(src);
      }
      return;
    }

    const ids = this.resolve().play; this.activeKey = ids.join(",");
    const bufs = await Promise.all(ids.map(id => this.load(id)));
    if (gen !== this._gen || !this.playing) return;   // superseded while decoding
    this.startedAt = this.ac.currentTime; this._starting = false;   // anchor clock to real audio start
    this.gainNodes = {};
    const isFull = ids.length === 1 && ids[0] === "full";
    this._mode = isFull ? "full" : "components";
    if (!(typeof Radio !== "undefined" && Radio.consumeFadeIn()))
      this.master.gain.value = this.masterVol;       // header dial = overall level
    for (let i = 0; i < ids.length; i++) {
      const buf = bufs[i]; if (!buf) continue;
      const src = this.ac.createBufferSource(); src.buffer = buf;
      if (isFull) {
        src.connect(this.master);
      } else {                                        // per-stem gain so faders work live
        const g = this.ac.createGain(); g.gain.value = this.rowVol(ids[i]);
        src.connect(g); g.connect(this.master);
        this.gainNodes[ids[i]] = g;
      }
      src.start(0, Math.min(off, buf.duration));
      this.sources.push(src);
    }
  },

  // Live volume change. Updates the running GainNode in place (no audio restart)
  // unless we cross the full<->components boundary, which needs a rebuild.
  setVol(id, v) {
    const r = S.rows.find(x => x.track.id === id); if (!r) return;
    r.vol = v;
    syncRowBtns();
    if (!this.playing) return;
    if (this.sceneMode) {
      const g = this.gainNodes[id]; if (!g) return;
      const aud = (typeof Scene !== "undefined") ? Scene.audibleSet() : "FULL";
      const on = (aud !== "FULL" && aud.has(id)) ? 1 : 0;
      g.gain.value = on * v; return;
    }
    const wantMode = (S.rows.some(x => x.solo || x.mute) || this.anyVolAdjusted())
      ? "components" : "full";
    if (wantMode !== this._mode) { this.refresh(); return; }   // rebuild once at boundary
    const g = this.gainNodes[id]; if (g) g.gain.value = v;
  },

  // scene mode plays each base instrument separately (so each gets an analyser)
  sceneIds() {
    const base = S.rows.filter(r => r.track.depth === 0 && r.track.id !== "mix").map(r => r.track.id);
    const solo = base.filter(id => S.rows.find(r => r.track.id === id).solo);
    const mute = new Set(base.filter(id => S.rows.find(r => r.track.id === id).mute));
    return solo.length ? solo : base.filter(id => !mute.has(id));
  },
  level(id) {                               // RMS amplitude 0..~1 for one instrument
    const a = this.analysers[id]; if (!a) return 0;
    const buf = this._td || (this._td = new Uint8Array(2048));
    a.getByteTimeDomainData(buf);
    let s = 0; for (let i = 0; i < buf.length; i++) { const x = (buf[i] - 128) / 128; s += x * x; }
    return Math.sqrt(s / buf.length);
  },
  bands(id) {                               // [low, mid, high] energy 0..1
    const a = this.analysers[id]; if (!a) return [0, 0, 0];
    const buf = this._fd || (this._fd = new Uint8Array(1024));
    a.getByteFrequencyData(buf);
    const t = Math.floor(buf.length / 3), avg = (s, e) => {
      let m = 0; for (let i = s; i < e; i++) m += buf[i]; return m / (e - s) / 255;
    };
    return [avg(0, t), avg(t, 2 * t), avg(2 * t, buf.length)];
  },
  pitchHz(id) {                             // monophonic fundamental via autocorrelation
    const a = this.analysers[id]; if (!a) return 0;
    const N = a.fftSize, b = this._pf && this._pf.length === N ? this._pf : (this._pf = new Float32Array(N));
    a.getFloatTimeDomainData(b);
    let rms = 0; for (let i = 0; i < N; i++) rms += b[i] * b[i]; rms = Math.sqrt(rms / N);
    if (rms < 0.012) return 0;
    const sr = this.ac.sampleRate, W = 512;
    const minLag = Math.floor(sr / 320), maxLag = Math.min(N - W - 1, Math.floor(sr / 50));
    let best = -1, bestv = 1e-9;
    for (let lag = minLag; lag <= maxLag; lag++) {
      let s = 0; for (let i = 0; i < W; i++) s += b[i] * b[i + lag];
      if (s > bestv) { bestv = s; best = lag; }
    }
    return best > 0 ? sr / best : 0;
  },
  setSceneMode(on) {
    this.sceneMode = on;
    if (this.playing) { this.offset = this.curTime(); this.startedAt = this.ac.currentTime; this.startSources(); }
  },

  async toggle() {
    if (!S.meta) return;
    if (this.playing) this.pause(); else await this.play();
  },
  async play() {
    Similar.stopPreview();               // don't overlap side-channel previews
    Galaxy.stopPreview();
    if (this.ac.state === "suspended") await this.ac.resume();
    this.offset = this.curTime(); this.startedAt = this.ac.currentTime; this.playing = true;
    els.play.textContent = "❚❚ pause"; els.play.classList.add("on");
    await this.startSources();
  },
  pause() {
    if (typeof Radio !== "undefined") Radio.cancelFade();
    this.offset = this.curTime(); this.stopSources(); this.playing = false;
    els.play.textContent = "▶ play"; els.play.classList.remove("on");
  },
  seek(t) {
    this.offset = clamp(t, 0, S.meta.duration - 0.01);
    if (this.playing) { this.startedAt = this.ac.currentTime; this.startSources(); }
    if (S.lock) centerOnPlayhead();
    this.drawPlayhead();
  },
  refresh() {                                  // solo/mute changed
    if (this.playing) { this.offset = this.curTime(); this.startedAt = this.ac.currentTime; this.startSources(); }
    updateReadout();
  },

  drawPlayhead() {
    const r = S.rows.find(x => S.visible.has(x.track.id));
    const w = r?.w || 1, t = this.curTime();
    if (t < S.view.start || t > S.view.end) { els.playhead.style.display = "none"; return; }
    els.playhead.style.display = "block"; els.playhead.style.left = timeToX(t, w) + "px";
  },
  tick() {
    if (S.meta) {
      this.drawPlayhead();
      Lyrics.update(this.curTime());
      TBar.update(this.curTime());
      if (this.playing) {
        const t = this.curTime(), span = S.view.end - S.view.start;
        if (Radio.on) {
          const left = S.meta.duration - t;
          if (!Radio._next && left < 20) Radio.pick();
          if (!Radio._fading && left <= Radio.FADE + 0.5) Radio.beginExit();
          else if (Radio._fading && left > Radio.FADE + 3) Radio.cancelFade();  // scrubbed back
        }
        if (t >= S.meta.duration - 0.02) { if (Radio.on) Radio.advance(); else this.pause(); }
        else if (S.lock)                                  // scroll lock: keep centered
          setView(t - span / 2, t + span / 2);
        else if (span < S.meta.duration * 0.9 && (t > S.view.end - span * 0.08 || t < S.view.start))
          setView(t - span * 0.5, t + span * 0.5);        // jump-follow
      }
    }
    requestAnimationFrame(() => this.tick());
  },
};

// ==========================================================================
// Ingest — drag-drop file + paste URL, with job toasts
// ==========================================================================
const Ingest = {
  setup() {
    els.addBtn.onclick = () => els.addPanel.hidden = !els.addPanel.hidden;
    document.querySelectorAll(".comp-wrap").forEach(() => {});
    document.addEventListener("click", e => {
      if (!e.target.closest(".comp-wrap") && !e.target.closest("#add-panel")) els.addPanel.hidden = true;
    });

    // click dropzone → file picker
    els.dropzone.onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file"; inp.accept = "audio/*";
      inp.onchange = () => inp.files[0] && this.uploadFile(inp.files[0]);
      inp.click();
    };
    // URL
    els.urlGo.onclick = () => this.submitUrl();
    els.urlInput.onkeydown = e => { if (e.key === "Enter") this.submitUrl(); };

    // whole-window drag & drop — self-healing: while a file is dragged over
    // the window, dragover keeps firing and resets a hide timer; the instant
    // the drag leaves or ends, the timer fires and the overlay disappears.
    // No enter/leave counting to desync, so it can never get stuck.
    let hideTimer = null;
    const isFileDrag = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    const hideDrop = () => { clearTimeout(hideTimer); els.dropOverlay.hidden = true; };
    window.addEventListener("dragover", e => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      els.dropOverlay.hidden = false;
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hideDrop, 200);
    });
    window.addEventListener("drop", e => {
      e.preventDefault(); hideDrop();
      const f = e.dataTransfer.files[0];
      if (f) this.uploadFile(f);
    });
    window.addEventListener("dragend", hideDrop);
    this.hideDrop = hideDrop;
  },

  ingestQuery() {
    const hq = document.querySelector("#hq-vocals")?.checked ? 1 : 0;
    const drums = document.querySelector("#drum-kit")?.checked ? 1 : 0;
    const six = document.querySelector("#six-stem")?.checked ? 1 : 0;
    return `?hq=${hq}&drums=${drums}&six=${six}`;
  },

  async uploadFile(file) {
    els.addPanel.hidden = true;
    const job = await fetch("/api/ingest_file" + this.ingestQuery(), {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(file.name).replace(/%20/g, " ") },
      body: file,
    }).then(r => r.json());
    this.track(job, file.name);
  },
  async submitUrl() {
    const url = els.urlInput.value.trim();
    if (!url) return;
    els.urlInput.value = ""; els.addPanel.hidden = true;
    const job = await fetch("/api/ingest_url" + this.ingestQuery(), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(r => r.json());
    this.track(job, url);
  },

  track(job, label) {
    if (!job || !job.id) { alert("Could not start: " + (job && job.error)); return; }
    const el = document.createElement("div");
    el.className = "job";
    el.innerHTML = `<div class="t"></div><div class="m"></div><div class="bar"><i></i></div>`;
    el.querySelector(".t").textContent = label;
    els.jobs.appendChild(el);
    const bar = el.querySelector(".bar"), fill = el.querySelector(".bar i");
    const poll = async () => {
      const j = await fetch("/api/job?id=" + job.id).then(r => r.json()).catch(() => null);
      if (!j) return setTimeout(poll, 1200);
      const pct = j.progress || 0;
      el.querySelector(".m").textContent = `${j.message || j.stage} · ${pct}%`;
      if (pct > 0) { bar.classList.add("determinate"); fill.style.width = pct + "%"; }
      if (j.title) el.querySelector(".t").textContent = j.title;
      if (j.stage === "done") {
        el.classList.add("done");
        el.querySelector(".m").textContent = "✓ ready — opening";
        refreshSongs(j.song_id);
        setTimeout(() => el.remove(), 4000);
      } else if (j.stage === "error") {
        el.classList.add("error");
        el.querySelector(".m").textContent = "✕ " + (j.error || "failed");
        setTimeout(() => el.remove(), 9000);
      } else setTimeout(poll, 1500);
    };
    poll();
  },
};

// ==========================================================================
// Live — realtime loopback spectrogram + waveform (BlackHole etc.)
// ==========================================================================
const Live = {
  on: false, ac: null, analyser: null, stream: null, raf: 0,
  freq: null, time: null, specCanvas: null, sctx: null,

  setup() {
    els.liveBtn.onclick = () => this.toggle();
    els.liveStart.onclick = () => this.start();
    els.liveStop.onclick = () => this.stop();
    els.liveHelpBtn.onclick = () => els.liveHelp.hidden = !els.liveHelp.hidden;
  },
  async toggle() {
    this.on = !this.on;
    document.body.classList.toggle("live", this.on);
    els.liveView.hidden = !this.on;
    els.liveBtn.classList.toggle("on", this.on);
    if (this.on) { await this.listDevices(); this.resize(); }
    // re-measure + redraw the stem view on exit: its canvases may have been built
    // while hidden (display:none → 0-sized), e.g. switching songs in live mode.
    else { this.stop(); requestAnimationFrame(() => { layout(); requestRender(true); }); }
  },
  async listDevices() {
    try { await navigator.mediaDevices.getUserMedia({ audio: true }).then(s => s.getTracks().forEach(t => t.stop())); } catch {}
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
    els.liveDevice.innerHTML = devs.map(d =>
      `<option value="${d.deviceId}">${d.label || "input " + d.deviceId.slice(0, 6)}</option>`).join("");
    const bh = devs.find(d => /blackhole/i.test(d.label));
    if (bh) els.liveDevice.value = bh.deviceId;
  },
  async start() {
    this.stop();
    const id = els.liveDevice.value;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: id ? { exact: id } : undefined, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (e) { alert("Could not open input: " + e.message); return; }
    this.ac = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.ac.createMediaStreamSource(this.stream);
    this.analyser = this.ac.createAnalyser();
    this.analyser.fftSize = 4096; this.analyser.smoothingTimeConstant = 0.5;
    src.connect(this.analyser);
    this.freq = new Uint8Array(this.analyser.frequencyBinCount);
    this.time = new Uint8Array(this.analyser.fftSize);
    els.liveStart.hidden = true; els.liveStop.hidden = false;
    this.resize();
    this.loop();
  },
  stop() {
    cancelAnimationFrame(this.raf);
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    if (this.ac) this.ac.close();
    this.stream = this.ac = this.analyser = null;
    els.liveStart.hidden = false; els.liveStop.hidden = true;
    els.liveLevel.style.width = "0%";
  },
  resize() {
    if (!this.on) return;
    const c = els.liveCanvas, dpr = DPR();
    const r = c.getBoundingClientRect();
    c.width = Math.max(1, Math.round(r.width * dpr));
    c.height = Math.max(1, Math.round(r.height * dpr));
    this.specCanvas = document.createElement("canvas");
    this.specCanvas.width = c.width; this.specCanvas.height = Math.max(1, c.height - 120 * dpr);
    this.sctx = this.specCanvas.getContext("2d");
    this.sctx.fillStyle = "#000"; this.sctx.fillRect(0, 0, this.specCanvas.width, this.specCanvas.height);
  },
  loop() {
    this.raf = requestAnimationFrame(() => this.loop());
    if (!this.analyser) return;
    this.analyser.getByteFrequencyData(this.freq);
    this.analyser.getByteTimeDomainData(this.time);
    const c = els.liveCanvas, ctx = c.getContext("2d"), W = c.width, H = c.height, dpr = DPR();
    const sc = this.specCanvas, sH = sc.height, sW = sc.width;

    // scroll spectrogram left by 2px, draw new column (log-frequency)
    const step = Math.max(1, Math.round(2 * dpr));
    this.sctx.drawImage(sc, -step, 0);
    const bins = this.freq.length, nyq = (this.ac.sampleRate || 48000) / 2;
    const fmin = 30, fmax = nyq;
    for (let y = 0; y < sH; y++) {
      const frac = 1 - y / sH;
      const f = fmin * Math.pow(fmax / fmin, frac);
      const bin = Math.min(bins - 1, Math.round(f / nyq * bins));
      const v = this.freq[bin] / 255;
      this.sctx.fillStyle = magmaCss(v);
      this.sctx.fillRect(sW - step, y, step, 1);
    }
    ctx.drawImage(sc, 0, 0);

    // waveform strip along the bottom
    const wy = sH, wh = H - sH;
    ctx.fillStyle = "#06070a"; ctx.fillRect(0, wy, W, wh);
    ctx.strokeStyle = "#2ecc71"; ctx.lineWidth = 1.5 * dpr; ctx.beginPath();
    const n = this.time.length;
    let peak = 0;
    for (let i = 0; i < n; i++) {
      const v = (this.time[i] - 128) / 128; peak = Math.max(peak, Math.abs(v));
      const x = i / n * W, yy = wy + wh / 2 - v * (wh / 2 - 4);
      i ? ctx.lineTo(x, yy) : ctx.moveTo(x, yy);
    }
    ctx.stroke();
    els.liveLevel.style.width = Math.min(100, peak * 140) + "%";
  },
};

function magmaCss(v) {
  const stops = [[0, 0, 4], [81, 18, 124], [183, 55, 121], [252, 137, 97], [252, 253, 191]];
  v = Math.max(0, Math.min(1, v)) * (stops.length - 1);
  const i = Math.floor(v), f = v - i, a = stops[i], b = stops[Math.min(stops.length - 1, i + 1)];
  return `rgb(${a[0] + (b[0] - a[0]) * f | 0},${a[1] + (b[1] - a[1]) * f | 0},${a[2] + (b[2] - a[2]) * f | 0})`;
}

// override readout to show live playback quality
const _updateReadout = updateReadout;
updateReadout = function () {
  _updateReadout();
  if (S.meta && (S.rows.some(r => r.solo || r.mute))) {
    els.readout.innerHTML += ` · <b>${Transport.quality()}</b>`;
  }
};

init();
