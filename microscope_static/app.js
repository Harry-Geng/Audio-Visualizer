"use strict";

// ---- track colors by group, tinted by depth ------------------------------
const GROUP_COLOR = {
  mix:    [210, 215, 225],
  drums:  [231, 76,  60],
  bass:   [52,  152, 219],
  vocals: [46,  204, 113],
  other:  [243, 156, 18],
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
  renderId: 0,
  _info: "",
};

const $ = s => document.querySelector(s);
const els = {
  song: $("#song"), mode: $("#mode"), play: $("#play"), fit: $("#fit"),
  zin: $("#zoomin"), zout: $("#zoomout"),
  compBtn: $("#comp-btn"), compPanel: $("#comp-panel"),
  readout: $("#readout"), ruler: $("#ruler"), tracks: $("#tracks"),
  overlay: $("#overlay"), playhead: $("#playhead"), cursor: $("#cursor"),
  scroll: $("#scroll"),
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

// ==========================================================================
// init
// ==========================================================================
async function init() {
  const songs = await fetch("/api/songs").then(r => r.json());
  els.song.innerHTML = songs.map(s => `<option value="${s.id}">${s.title}</option>`).join("");
  els.song.onchange = () => loadSong(els.song.value);

  els.mode.querySelectorAll("button").forEach(b => b.onclick = () => {
    S.mode = b.dataset.mode;
    els.mode.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    S.rows.forEach(r => { r.cache = null; r.specCache = null; });
    requestRender(true);
  });
  els.play.onclick = () => Transport.toggle();
  els.fit.onclick = () => setView(0, S.meta.duration);
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
  S._info = [f.tempo ? Math.round(f.tempo) + " bpm" : "", keyName, q].filter(Boolean).join(" · ");

  S.visible = new Set(S.meta.tracks.map(t => t.id));   // all on by default
  buildRows();
  buildCompPanel();
  Transport.reset();
  applyVisibility();
  setView(0, S.meta.duration);
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
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML =
      `<div class="label">
         <div class="nm ${t.depth ? "child" : ""}" style="color:${t.depth ? rgb(c) : "#fff"}">${t.label}</div>
         <div class="sm">
           <button class="solo" title="solo (hear only this)">S</button>
           <button class="mute" title="mute">M</button>
         </div>
       </div>
       <div class="cv"><canvas></canvas></div>`;
    els.tracks.appendChild(row);
    const canvas = row.querySelector("canvas");
    const r = {
      track: t, el: row, canvas, ctx: canvas.getContext("2d"),
      color: c, w: 1, h: 1, cache: null, specCache: null, solo: false, mute: false,
    };
    row.querySelector(".solo").onclick = () => { r.solo = !r.solo; syncRowBtns(); Transport.refresh(); };
    row.querySelector(".mute").onclick = () => { r.mute = !r.mute; syncRowBtns(); Transport.refresh(); };
    S.rows.push(r);
  }
}
function syncRowBtns() {
  const dim = Transport.resolve ? Transport.resolve().dim : new Set();
  for (const r of S.rows) {
    r.el.querySelector(".solo").classList.toggle("on", r.solo);
    r.el.querySelector(".mute").classList.toggle("on", r.mute);
    r.el.classList.toggle("dim", dim.has(r.track.id));
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
const refetch = debounce(_refetch, 110);
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
  if ((f.beats || []).length && span < 40) {
    ctx.strokeStyle = "rgba(255,255,255,.10)"; ctx.beginPath();
    for (const b of f.beats) {
      if (b < S.view.start || b > S.view.end) continue;
      const x = timeToX(b, w); ctx.moveTo(x, 0); ctx.lineTo(x, h);
    }
    ctx.stroke();
  }
  for (const p of (f.phrases || [])) {
    if (p < S.view.start || p > S.view.end) continue;
    const x = timeToX(p, w);
    ctx.strokeStyle = "rgba(120,200,255,.35)"; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); ctx.setLineDash([]);
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
    e.preventDefault();
    const w = vw(), x = clamp(localX(e.clientX), 0, w);
    if (e.ctrlKey || e.metaKey) {                       // pinch / cmd+scroll = zoom
      zoomBy(Math.exp(clamp(e.deltaY, -40, 40) * 0.01), xToTime(x, w));
    } else {                                            // swipe = pan
      const span = S.view.end - S.view.start;
      const dom = Math.abs(e.deltaX) >= Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      const d = dom / w * span;
      setView(S.view.start + d, S.view.end + d);
    }
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
// WebAudio transport with solo / mute over decomposed components
// ==========================================================================
const Transport = {
  ac: null, master: null, buffers: new Map(), loading: new Map(),
  sources: [], playing: false, offset: 0, startedAt: 0, activeKey: "",

  init() {
    this.ac = new (window.AudioContext || window.webkitAudioContext)();
    this.master = this.ac.createGain(); this.master.connect(this.ac.destination);
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
    if (!anyMute && !anySolo) return { play: ["full"], dim };          // untouched → original
    const mixRow = byId("mix");
    if (mixRow && mixRow.solo) {                                       // solo mix = whole song
      rows.forEach(r => { if (r.track.id !== "mix") dim.add(r.track.id); });
      return { play: ["full"], dim };
    }
    const groups = STEMS.filter(s => byId(s));
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
    const t = this.playing ? this.offset + (this.ac.currentTime - this.startedAt) : this.offset;
    return clamp(t, 0, S.meta ? S.meta.duration : 0);
  },
  stopSources() { for (const s of this.sources) { try { s.stop(); } catch {} } this.sources = []; },

  async startSources() {
    this.stopSources();
    const ids = this.resolve().play; this.activeKey = ids.join(",");
    const off = this.offset;
    const bufs = await Promise.all(ids.map(id => this.load(id)));
    if (!this.playing || this.activeKey !== ids.join(",")) return;
    for (const buf of bufs) {
      if (!buf) continue;
      const src = this.ac.createBufferSource();
      src.buffer = buf; src.connect(this.master); src.start(0, Math.min(off, buf.duration));
      this.sources.push(src);
    }
  },

  async toggle() {
    if (!S.meta) return;
    if (this.playing) this.pause(); else await this.play();
  },
  async play() {
    if (this.ac.state === "suspended") await this.ac.resume();
    this.offset = this.curTime(); this.startedAt = this.ac.currentTime; this.playing = true;
    els.play.textContent = "❚❚ pause"; els.play.classList.add("on");
    await this.startSources();
  },
  pause() {
    this.offset = this.curTime(); this.stopSources(); this.playing = false;
    els.play.textContent = "▶ play"; els.play.classList.remove("on");
  },
  seek(t) {
    this.offset = clamp(t, 0, S.meta.duration - 0.01);
    if (this.playing) { this.startedAt = this.ac.currentTime; this.startSources(); }
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
      if (this.playing) {
        const t = this.curTime(), span = S.view.end - S.view.start;
        if (t >= S.meta.duration - 0.02) this.pause();
        else if (span < S.meta.duration * 0.9 && (t > S.view.end - span * 0.08 || t < S.view.start))
          setView(t - span * 0.5, t + span * 0.5);
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
    return `?hq=${hq}&drums=${drums}`;
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
    else this.stop();
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
