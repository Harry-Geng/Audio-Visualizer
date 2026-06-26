"use strict";
// Drum-kit piece layout (fractions of the kit sub-rect) + note names for the stage.
const DRUM_LAYOUT = {
  kick:  { x: 0.50, y: 0.82, r: 0.17,  type: "drum",   col: [231, 76, 60] },
  snare: { x: 0.34, y: 0.64, r: 0.085, type: "drum",   col: [241, 196, 15] },
  toms:  { x: 0.54, y: 0.50, r: 0.075, type: "drum",   col: [230, 126, 34] },
  hh:    { x: 0.18, y: 0.52, r: 0.080, type: "cymbal", col: [170, 205, 235] },
  hat:   { x: 0.18, y: 0.52, r: 0.080, type: "cymbal", col: [170, 205, 235] },
  ride:  { x: 0.80, y: 0.44, r: 0.115, type: "cymbal", col: [241, 210, 120] },
  crash: { x: 0.30, y: 0.34, r: 0.105, type: "cymbal", col: [200, 220, 255] },
};
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// Scene mode — a performing-ensemble "stage" plus prototype audio-reactive views
// stems. Pure canvas 2D (no deps). Driven by live per-instrument analysers from
// Transport (set up in app.js) + beat data. Reuses app.js globals: S, Transport,
// els, clamp, trackColor, syncRowBtns.

const Scene = {
  on: false, canvas: null, ctx: null, hud: null, instsEl: null, btn: null,
  raf: 0, style: "stage",
  W: 1, H: 1, dpr: 1, clock: 0, dt: 0, t0: 0, lastNow: 0,
  yaw: 0.5, pitch: 0.28, autoYaw: 0, dragging: false, lastX: 0, lastY: 0,
  insts: [], particles: [], events: [], builtFor: null,
  kit: {}, chroma: new Array(12).fill(0), voxHz: 0, voxLvl: 0, voxTrail: [], voxHist: [], bassHz: 0,
  frame: 0, voxXs: 0, voxYs: 0, voxLvlS: 0, pitchBuf: [],
  // WebGL mandala (lazy-initialised on first use)
  gl: null, glProg: null, glCanvas: null, glU: null, glFailed: false, mandalaRot: 0, _chromaArr: null,

  _lerp(a, b, t) { return a + (b - a) * t; },

  // every instrument stem we want an analyser for (drums expand into kit pieces)
  allInstrumentIds() {
    const out = [];
    for (const r of S.rows) {
      if (r.track.depth !== 0 || r.track.id === "mix") continue;
      if (r.track.id === "drums") { const p = this.drumParts(); out.push(...(p.length ? p : ["drums"])); }
      else out.push(r.track.id);
    }
    return out;
  },
  // which instruments should be AUDIBLE: "FULL" (play the original) unless the
  // user is soloing/muting, in which case the relevant stems play instead.
  audibleSet() {
    const base = S.rows.filter(r => r.track.depth === 0 && r.track.id !== "mix");
    const solo = base.filter(r => r.solo).map(r => r.track.id);
    const mute = new Set(base.filter(r => r.mute).map(r => r.track.id));
    let chosen;
    if (solo.length) chosen = base.filter(r => solo.includes(r.track.id));
    else if (mute.size) chosen = base.filter(r => !mute.has(r.track.id));
    else return "FULL";
    const set = new Set();
    for (const r of chosen) {
      if (r.track.id === "drums") { const p = this.drumParts(); (p.length ? p : ["drums"]).forEach(x => set.add(x)); }
      else set.add(r.track.id);
    }
    return set;
  },

  setup() {
    this.canvas = document.getElementById("scene-canvas");
    this.ctx = this.canvas.getContext("2d");
    this.hud = document.getElementById("scene");
    this.instsEl = document.getElementById("scene-insts");
    this.btn = document.getElementById("scene-btn");
    this.btn.onclick = () => this.toggle();

    document.querySelectorAll("#scene-styles button").forEach(b => {
      b.onclick = () => {
        this.style = b.dataset.s;
        document.querySelectorAll("#scene-styles button").forEach(x => x.classList.toggle("on", x === b));
        this.events.length = 0; this.particles.length = 0;
      };
    });

    const c = this.canvas;
    c.addEventListener("pointerdown", e => { this.dragging = true; this.lastX = e.clientX; this.lastY = e.clientY; c.setPointerCapture(e.pointerId); });
    c.addEventListener("pointermove", e => {
      if (!this.dragging) return;
      this.yaw += (e.clientX - this.lastX) * 0.01;
      this.pitch = clamp(this.pitch + (e.clientY - this.lastY) * 0.005, -0.3, 1.1);
      this.lastX = e.clientX; this.lastY = e.clientY;
    });
    c.addEventListener("pointerup", () => this.dragging = false);
    window.addEventListener("resize", () => { if (this.on) this.resize(); });
  },

  toggle() {
    this.on = !this.on;
    document.body.classList.toggle("scene", this.on);
    this.hud.hidden = !this.on;
    this.btn.classList.toggle("on", this.on);
    Transport.setSceneMode(this.on);
    if (this.on) {
      this.build(); this.resize();
      this.t0 = performance.now(); this.lastNow = this.t0;
      cancelAnimationFrame(this.raf); this.loop();
    } else {
      cancelAnimationFrame(this.raf);
    }
  },

  resize() {
    this.dpr = window.devicePixelRatio || 1;
    const r = this.canvas.getBoundingClientRect();
    this.W = Math.max(1, Math.round(r.width));
    this.H = Math.max(1, Math.round(r.height));
    this.canvas.width = Math.round(this.W * this.dpr);
    this.canvas.height = Math.round(this.H * this.dpr);
  },

  build() {
    if (!S.meta) return;
    const base = S.meta.tracks.filter(t => t.depth === 0 && t.id !== "mix");
    const N = Math.max(1, base.length);
    const REG = { sub: -1, bass: -1, drums: -0.3, kick: -0.5, other: 0.2, guitar: 0.45, piano: 0.55, vocals: 1 };
    this.insts = base.map((t, i) => {
      const c = (typeof trackColor === "function") ? trackColor(t) : [200, 200, 220];
      const ang = i / N * Math.PI * 2;
      const reg = REG[t.id] != null ? REG[t.id] : 0;
      return {
        id: t.id, label: t.label, color: c, ang, reg, idx: i,
        pos: [Math.cos(ang) * 3, reg * 2.0, Math.sin(ang) * 3],
        lvl: 0, smooth: 0, prev: 0, onset: 0, bands: [0, 0, 0],
      };
    });
    this.beats = (S.meta.features && S.meta.features.beats) || [];
    this.builtFor = S.songId;
    this.buildHUD();
  },

  buildHUD() {
    this.instsEl.innerHTML = this.insts.map(o =>
      `<div class="ic" data-id="${o.id}"><span class="dot" style="background:rgb(${o.color[0]},${o.color[1]},${o.color[2]})"></span>${o.label}</div>`
    ).join("");
    this.instsEl.querySelectorAll(".ic").forEach(el => {
      el.onclick = () => {
        const r = S.rows.find(x => x.track.id === el.dataset.id);
        if (!r) return;
        r.solo = !r.solo;
        if (typeof syncRowBtns === "function") syncRowBtns();
        Transport.refresh();
        this.syncHUD();
      };
    });
  },
  syncHUD() {
    this.instsEl.querySelectorAll(".ic").forEach(el => {
      const r = S.rows.find(x => x.track.id === el.dataset.id);
      el.classList.toggle("solo", !!(r && r.solo));
      el.classList.toggle("muted", !!(r && r.mute));
    });
  },

  drumParts() {
    if (!S.meta) return [];
    return S.meta.tracks.filter(t => t.group === "drums" && t.depth === 1).map(t => t.id);
  },
  inst(id) { return this.insts.find(o => o.id === id); },

  sample() {
    const t = Transport.curTime ? Transport.curTime() : 0;
    let beat = 0;
    if (this.beats && this.beats.length) {
      let lb = 0;
      for (const b of this.beats) { if (b <= t) lb = b; else break; }
      beat = Math.exp(-(t - lb) / 0.16);
    }
    const parts = this.drumParts();
    for (const o of this.insts) {
      let lv;
      if (o.id === "drums" && parts.length) { lv = 0; for (const p of parts) lv = Math.max(lv, Transport.level(p)); }
      else lv = Transport.level(o.id);
      o.bands = Transport.bands(o.id);
      o.smooth = o.smooth * 0.7 + lv * 0.3;
      o.onset = Math.max(0, lv - o.prev - 0.04);
      o.prev = o.prev * 0.6 + lv * 0.4;
      o.lvl = lv;
    }
    // per-drum-piece hits (decaying), for the stage kit
    for (const p of parts) {
      const k = this.kit[p] || (this.kit[p] = { hit: 0, prev: 0 });
      const lv = Transport.level(p);
      const onset = Math.max(0, lv - k.prev - 0.05);
      k.prev = k.prev * 0.5 + lv * 0.5;
      k.hit = Math.max(k.hit * 0.86, onset > 0.05 ? 1 : 0);
    }
    // harmony chroma from the most harmonic stem available
    const harm = ["piano", "guitar", "other"].find(id => Transport.analysers[id]);
    this.chroma = harm ? this.computeChroma(harm) : this.chroma.map(v => v * 0.9);
    // vocal pitch line
    this.voxLvl = Transport.level("vocals");
    this.voxHz = this.sampleVocalHz(t);
    this.voxTrail.push(this.voxHz);
    if (this.voxTrail.length > 220) this.voxTrail.shift();
    // bass fundamental — every 3rd frame, gated by level, heavily smoothed
    const bl = (this.inst("bass") || {}).smooth || 0;
    if (this.frame % 3 === 0 && bl > 0.03) {
      const bh = Transport.pitchHz("bass");
      if (bh > 30 && bh < 400) this.bassHz = this.bassHz ? this.bassHz * 0.7 + bh * 0.3 : bh;
    }
    // rolling pitch history for the pitch-roll view (0 = silent/unvoiced → gap)
    this.pitchBuf.push({ vox: this.voxHz, bass: bl > 0.03 ? this.bassHz : 0 });
    if (this.pitchBuf.length > 480) this.pitchBuf.shift();
    return { t, beat };
  },

  computeChroma(id) {
    const a = Transport.analysers[id]; if (!a) return this.chroma;
    const buf = Transport._fd || (Transport._fd = new Uint8Array(512));
    a.getByteFrequencyData(buf);
    const sr = Transport.ac.sampleRate, nfft = a.fftSize, ch = new Array(12).fill(0);
    for (let bin = 1; bin < buf.length; bin++) {
      const fr = bin * sr / nfft;
      if (fr < 55 || fr > 2000) continue;
      const midi = 69 + 12 * Math.log2(fr / 440);
      ch[((Math.round(midi) % 12) + 12) % 12] += buf[bin] / 255;
    }
    const mx = Math.max(...ch, 1e-6);
    return ch.map((v, i) => Math.max(this.chroma[i] * 0.82, v / mx));
  },
  sampleVocalHz(t) {
    const f = S.meta && S.meta.features;
    if (!f || !f.pitch || !f.pitch.vocals) return 0;
    const hz = f.pitch.vocals.hz, voiced = f.pitch.vocals.voiced;
    if (!hz || !hz.length) return 0;
    const i = Math.floor(t * (f.fps || 43.066));
    if (i < 0 || i >= hz.length) return 0;
    if (voiced && voiced[i] < 0.5) return 0;
    return hz[i] || 0;
  },

  // ---- 6. Kaleidoscope mandala (WebGL) ----
  // 12 chroma bins drive 12 mirrored petals; beat pulses the rings, bass breathes
  // the center, overall energy lights the core, and hue cycles with time.
  MANDALA_FS: `
    precision highp float;
    uniform vec2 u_res;
    uniform float u_time;
    uniform float u_chroma[12];
    uniform float u_beat;
    uniform float u_bass;
    uniform float u_energy;
    uniform float u_rot;
    #define TAU 6.28318530718
    vec3 hsv(float h, float s, float v){
      vec3 c = clamp(abs(mod(h*6.0 + vec3(0.0,4.0,2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
      return v * mix(vec3(1.0), c, s);
    }
    float chromaAt(int idx){
      for (int i = 0; i < 12; i++){ if (i == idx) return u_chroma[i]; }
      return 0.0;
    }
    void main(){
      vec2 uv = (gl_FragCoord.xy - 0.5 * u_res) / min(u_res.x, u_res.y);
      float r = length(uv);
      r *= 1.0 + u_bass * 0.45 * sin(r * 7.0 - u_time * 1.6);   // bass breathes the center
      float a = atan(uv.y, uv.x) + u_rot;
      float sector = TAU / 12.0;
      float si = floor(a / sector + 0.5);
      float folded = abs(a - si * sector);                     // mirror -> 24-fold symmetry
      int idx = int(mod(si, 12.0));
      float e = chromaAt(idx);
      vec3 col = vec3(0.0);
      for (int k = 0; k < 5; k++){
        float fk = float(k);
        float ringR = 0.13 + fk * 0.17;
        float pulse = ringR * (1.0 + u_beat * 0.10) + e * 0.05;
        float petalW = 0.04 + e * 0.16;
        float band = exp(-pow((r - pulse) / petalW, 2.0));
        float angW = 0.05 + e * 0.40;
        float ang = exp(-pow(folded / angW, 2.0));
        float m = band * ang * (0.25 + e * 1.6);
        float hue = fract(float(idx) / 12.0 + u_time * 0.015 + fk * 0.04);
        col += hsv(hue, 0.75, 1.0) * m;
      }
      col += hsv(fract(u_time * 0.025), 0.35, 1.0) * u_energy * 0.6 * exp(-r * r * 7.0);  // core glow
      col *= smoothstep(1.5, 0.15, r);                         // vignette
      gl_FragColor = vec4(col, 1.0);
    }`,

  _glShader(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.error("shader:", gl.getShaderInfoLog(s)); return null; }
    return s;
  },

  ensureGL() {
    if (this.gl) return this.gl;
    if (this.glFailed) return null;
    const c = this.glCanvas = document.getElementById("scene-gl");
    const gl = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (!gl) { this.glFailed = true; return null; }
    const vs = this._glShader(gl, gl.VERTEX_SHADER, "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }");
    const fs = this._glShader(gl, gl.FRAGMENT_SHADER, this.MANDALA_FS);
    if (!vs || !fs) { this.glFailed = true; return null; }
    const prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { console.error("link:", gl.getProgramInfoLog(prog)); this.glFailed = true; return null; }
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW); // full-screen triangle
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.glU = {
      res: gl.getUniformLocation(prog, "u_res"), time: gl.getUniformLocation(prog, "u_time"),
      chroma: gl.getUniformLocation(prog, "u_chroma[0]"), beat: gl.getUniformLocation(prog, "u_beat"),
      bass: gl.getUniformLocation(prog, "u_bass"), energy: gl.getUniformLocation(prog, "u_energy"),
      rot: gl.getUniformLocation(prog, "u_rot"),
    };
    this.gl = gl; this.glProg = prog;
    return gl;
  },

  renderMandala(f) {
    const gl = this.ensureGL();
    if (!gl) {  // WebGL unavailable — keep the 2D canvas with a notice
      this.canvas.style.display = "block";
      if (this.glCanvas) this.glCanvas.style.display = "none";
      const ctx = this.ctx; ctx.fillStyle = "#06070d"; ctx.fillRect(0, 0, this.W, this.H);
      ctx.fillStyle = "rgba(255,255,255,0.5)"; ctx.font = "13px ui-monospace, monospace"; ctx.textAlign = "center";
      ctx.fillText("WebGL unavailable — mandala needs it", this.W / 2, this.H / 2); ctx.textAlign = "left";
      return;
    }
    this.canvas.style.display = "none";
    this.glCanvas.style.display = "block";
    const w = Math.max(1, Math.round(this.W * this.dpr)), h = Math.max(1, Math.round(this.H * this.dpr));
    if (this.glCanvas.width !== w || this.glCanvas.height !== h) { this.glCanvas.width = w; this.glCanvas.height = h; }
    gl.viewport(0, 0, w, h);
    this.mandalaRot += this.dt * 0.08 + f.beat * 0.05;          // drift + a kick on every beat
    let energy = 0; for (const o of this.insts) energy += o.smooth; energy /= Math.max(1, this.insts.length);
    const bass = (this.inst("bass") || {}).smooth || 0;
    if (!this._chromaArr) this._chromaArr = new Float32Array(12);
    for (let i = 0; i < 12; i++) this._chromaArr[i] = this.chroma[i] || 0;
    const U = this.glU;
    gl.useProgram(this.glProg);
    gl.uniform2f(U.res, w, h);
    gl.uniform1f(U.time, this.clock);
    gl.uniform1fv(U.chroma, this._chromaArr);
    gl.uniform1f(U.beat, f.beat);
    gl.uniform1f(U.bass, Math.min(1, bass * 1.5));
    gl.uniform1f(U.energy, Math.min(1, energy * 1.5));
    gl.uniform1f(U.rot, this.mandalaRot);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  },

  loop() {
    this.raf = requestAnimationFrame(() => this.loop());
    if (!this.on) return;
    const now = performance.now();
    this.clock = (now - this.t0) / 1000;
    this.dt = Math.min(0.05, (now - this.lastNow) / 1000); this.lastNow = now;
    if (S.meta && this.builtFor !== S.songId) this.build();

    const ctx = this.ctx, W = this.W, H = this.H;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    if (!this.insts.length) { ctx.fillStyle = "#04050a"; ctx.fillRect(0, 0, W, H); return; }
    this.autoYaw += 0.0015; this.frame++;
    const f = this.sample();
    // mandala is rendered on a separate WebGL canvas; everything else on the 2D one
    if (this.style === "mandala") { this.renderMandala(f); this.syncHUD(); return; }
    if (this.glCanvas) this.glCanvas.style.display = "none";
    this.canvas.style.display = "block";
    if (this.style === "stage") this.renderStage(ctx, W, H, f);
    else if (this.style === "pitch") this.renderPitch(ctx, W, H, f);
    else if (this.style === "constellation") this.renderConstellation(ctx, W, H, f);
    else if (this.style === "field") this.renderField(ctx, W, H, f);
    else if (this.style === "roll") this.renderRoll(ctx, W, H, f);
    else this.renderGeometry(ctx, W, H, f);
    this.syncHUD();
  },

  project(p, W, H) {
    const yaw = this.yaw + this.autoYaw, cy = Math.cos(yaw), sy = Math.sin(yaw);
    const x = p[0] * cy - p[2] * sy, z1 = p[0] * sy + p[2] * cy, y = p[1];
    const cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    const y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
    const camD = 7, zz = z2 + camD, fl = H * 0.9 / Math.max(0.1, zz);
    return { x: W / 2 + x * fl, y: H / 2 - y2 * fl, scale: camD / Math.max(0.1, zz), z: zz };
  },

  // ---- 1. constellation of orbs ----
  renderConstellation(ctx, W, H, f) {
    ctx.fillStyle = "rgba(4,5,10,0.30)"; ctx.fillRect(0, 0, W, H);  // motion trails
    const ps = this.insts.map(o => ({ o, p: this.project(o.pos, W, H) })).sort((a, b) => b.p.z - a.p.z);
    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < ps.length; i++) for (let j = i + 1; j < ps.length; j++) {
      const a = ps[i], b = ps[j], e = (a.o.smooth + b.o.smooth) * 0.5;
      if (e < 0.02) continue;
      ctx.strokeStyle = `rgba(170,195,255,${Math.min(0.3, e * 0.6 * (0.4 + f.beat))})`;
      ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.p.x, a.p.y); ctx.lineTo(b.p.x, b.p.y); ctx.stroke();
    }
    for (const { o, p } of ps) {
      const r = Math.max(2, 18 * p.scale * (0.5 + o.smooth * 3 + f.beat * 0.3));
      const [cr, cg, cb] = o.color, a = Math.min(1, 0.5 + o.smooth * 0.9);
      const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
      g.addColorStop(0, `rgba(${cr},${cg},${cb},${a})`);
      g.addColorStop(0.4, `rgba(${cr},${cg},${cb},${a * 0.45})`);
      g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill();
      if (o.onset > 0.05) for (let k = 0; k < 10; k++)
        this.particles.push({ x: p.x, y: p.y, vx: (Math.random() - 0.5) * 5, vy: (Math.random() - 0.5) * 5, life: 1, c: o.color });
    }
    this.particles = this.particles.filter(pt => pt.life > 0);
    for (const pt of this.particles) {
      pt.x += pt.vx; pt.y += pt.vy; pt.vx *= 0.96; pt.vy *= 0.96; pt.life -= 0.03;
      ctx.fillStyle = `rgba(${pt.c[0]},${pt.c[1]},${pt.c[2]},${pt.life})`;
      ctx.fillRect(pt.x - 1.5, pt.y - 1.5, 3, 3);
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.font = "11px ui-monospace, monospace"; ctx.textAlign = "center";
    for (const { o, p } of ps) {
      ctx.fillStyle = `rgba(${o.color[0]},${o.color[1]},${o.color[2]},0.8)`;
      ctx.fillText(o.label, p.x, p.y + Math.max(2, 18 * p.scale) + 14);
    }
    ctx.textAlign = "left";
  },

  // ---- 2. flowing liquid-light field ----
  renderField(ctx, W, H, f) {
    ctx.fillStyle = "rgba(4,5,10,0.16)"; ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    const tm = this.clock, minD = Math.min(W, H);
    this.insts.forEach((o, i) => {
      const e = o.smooth, [cr, cg, cb] = o.color;
      const cx = W / 2 + Math.cos(tm * 0.3 + o.ang) * W * 0.30 + Math.sin(tm * 0.17 + i) * W * 0.07;
      const cy = H / 2 + Math.sin(tm * 0.23 + o.ang * 1.3) * H * 0.28 - o.reg * H * 0.10;
      const R = minD * (0.12 + e * 0.55 + f.beat * 0.04);
      const a = Math.min(0.9, 0.07 + e * 0.7);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(4, R));
      g.addColorStop(0, `rgba(${cr},${cg},${cb},${a})`);
      g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, Math.max(4, R), 0, 7); ctx.fill();
      const hi = o.bands[2];
      if (hi > 0.15) for (let k = 0; k < hi * 24; k++) {
        const sx = cx + (Math.random() - 0.5) * R, sy = cy + (Math.random() - 0.5) * R;
        ctx.fillStyle = `rgba(${cr},${cg},${cb},${hi})`; ctx.fillRect(sx, sy, 2, 2);
      }
    });
    ctx.globalCompositeOperation = "source-over";
  },

  // ---- 3. 3D piano-roll ----
  renderRoll(ctx, W, H, f) {
    ctx.fillStyle = "rgba(4,5,10,0.25)"; ctx.fillRect(0, 0, W, H);
    const horizon = H * 0.36, N = this.insts.length;
    this.insts.forEach((o, i) => {
      if (o.onset > 0.05) this.events.push({ i, z: 1, lvl: o.smooth + 0.3, c: o.color });
    });
    // perspective lane guides
    ctx.strokeStyle = "rgba(120,140,200,0.08)"; ctx.lineWidth = 1;
    for (let i = 0; i < N; i++) {
      const xl = (i / (N - 1 || 1)) * 2 - 1;
      ctx.beginPath();
      ctx.moveTo(W / 2 + xl * 0.06 * W, horizon);
      ctx.lineTo(W / 2 + xl * 0.46 * W, H);
      ctx.stroke();
    }
    this.events.forEach(ev => ev.z -= this.dt * 0.35);
    this.events = this.events.filter(ev => ev.z > -0.05);
    this.events.sort((a, b) => b.z - a.z);
    for (const ev of this.events) {
      const near = 1 - ev.z;
      const y = horizon + near * (H * 0.97 - horizon);
      const xl = (ev.i / (N - 1 || 1)) * 2 - 1;
      const x = W / 2 + xl * this._lerp(0.06, 0.46, near) * W;
      const s = this._lerp(3, 28, near) * (0.4 + ev.lvl);
      const fade = ev.z < 0.08 ? ev.z * 12 : 1;
      const [cr, cg, cb] = ev.c;
      ctx.globalCompositeOperation = "lighter";
      ctx.fillStyle = `rgba(${cr},${cg},${cb},${Math.min(1, fade) * (0.35 + near * 0.6)})`;
      ctx.fillRect(x - s / 2, y - s / 2, s, s);
    }
    ctx.globalCompositeOperation = "source-over";
  },

  // ---- 4. reactive sacred-geometry ----
  renderGeometry(ctx, W, H, f) {
    ctx.fillStyle = "rgba(4,5,10,0.20)"; ctx.fillRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2, tm = this.clock;
    let energy = 0; for (const o of this.insts) energy += o.smooth;
    energy /= Math.max(1, this.insts.length);
    const N = 10, R = Math.min(W, H) * 0.5 * (0.45 + energy * 0.7 + f.beat * 0.15);
    ctx.globalCompositeOperation = "lighter";
    for (let k = 0; k < N; k++) {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(tm * 0.18 + k * Math.PI * 2 / N);
      this.insts.forEach((o, i) => {
        const e = o.smooth, [cr, cg, cb] = o.color;
        const petal = R * (0.2 + i / this.insts.length * 0.8) * (0.5 + e * 0.9);
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${0.08 + e * 0.7})`;
        ctx.lineWidth = 1 + e * 4;
        for (const m of [1, -1]) {
          ctx.beginPath(); ctx.moveTo(0, 0);
          ctx.quadraticCurveTo(m * petal * 0.5, -petal * 0.6, 0, -petal);
          ctx.quadraticCurveTo(-m * petal * 0.5, -petal * 0.6, 0, 0);
          ctx.stroke();
        }
      });
      ctx.restore();
    }
    ctx.globalCompositeOperation = "source-over";
  },

  // ---- 5. the performing stage (drum kit + vocals + bass + keyboard) ----
  renderStage(ctx, W, H, f) {
    const bg = ctx.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#06070d"); bg.addColorStop(1, "#0a0b16");
    ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);
    this.drawVocals(ctx, W, H, f);
    this.drawBass(ctx, W, H, f);
    this.drawDrumKit(ctx, W, H, f);
  },

  // ---- pitch-roll: clean melodic contours of vocals + bass over time ----
  renderPitch(ctx, W, H, f) {
    ctx.fillStyle = "#06070d"; ctx.fillRect(0, 0, W, H);
    const top = H * 0.05, bot = H * 0.95, hgt = bot - top, FLO = 49, FHI = 784; // G1..G5
    const L2 = Math.log2, lo = L2(FLO), span = L2(FHI) - lo;
    const yOf = hz => top + (1 - (L2(hz) - lo) / span) * hgt;
    // staff lines: every semitone faint, every C brighter + labelled
    const m0 = Math.ceil(12 * L2(FLO / 440) + 69), m1 = Math.floor(12 * L2(FHI / 440) + 69);
    for (let midi = m0; midi <= m1; midi++) {
      const y = yOf(440 * Math.pow(2, (midi - 69) / 12)), isC = ((midi % 12) + 12) % 12 === 0;
      ctx.strokeStyle = isC ? "rgba(255,255,255,0.11)" : "rgba(255,255,255,0.035)";
      ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      if (isC) { ctx.fillStyle = "rgba(255,255,255,0.30)"; ctx.font = "10px ui-monospace, monospace"; ctx.fillText("C" + (Math.floor(midi / 12) - 1), 5, y - 2); }
    }
    const N = this.pitchBuf.length;
    // draw a key's contour as smooth curves through moving-averaged points,
    // split into contiguous voiced segments (gaps where silent/unvoiced)
    const line = (key, color, lw) => {
      ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.lineJoin = "round"; ctx.lineCap = "round";
      let seg = [];
      const flush = () => {
        if (seg.length >= 2) {
          const ys = seg.map(p => p.y), sm = ys.slice(), w = 3;
          for (let i = 0; i < ys.length; i++) { let s = 0, c = 0; for (let j = -w; j <= w; j++) { const k = i + j; if (k >= 0 && k < ys.length) { s += ys[k]; c++; } } sm[i] = s / c; }
          ctx.beginPath(); ctx.moveTo(seg[0].x, sm[0]);
          for (let i = 1; i < seg.length - 1; i++) { const mx = (seg[i].x + seg[i + 1].x) / 2, my = (sm[i] + sm[i + 1]) / 2; ctx.quadraticCurveTo(seg[i].x, sm[i], mx, my); }
          ctx.lineTo(seg[seg.length - 1].x, sm[sm.length - 1]); ctx.stroke();
        }
        seg = [];
      };
      for (let i = 0; i < N; i++) {
        const v = this.pitchBuf[i][key], x = (i / (N - 1 || 1)) * W;
        if (v < FLO || v > FHI * 1.02) { flush(); continue; }
        seg.push({ x, y: yOf(clamp(v, FLO, FHI)) });
      }
      flush();
    };
    ctx.globalCompositeOperation = "lighter";
    line("bass", "rgba(52,152,219,0.22)", 10); line("vox", "rgba(46,204,113,0.22)", 10);  // glow
    line("bass", "rgba(130,200,255,0.95)", 2.5); line("vox", "rgba(130,255,185,0.95)", 2.5);
    ctx.globalCompositeOperation = "source-over";
    // now-line + current note markers at the right edge
    ctx.strokeStyle = "rgba(255,255,255,0.22)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(W - 1, top); ctx.lineTo(W - 1, bot); ctx.stroke();
    const last = this.pitchBuf[N - 1] || { vox: 0, bass: 0 };
    for (const [key, col] of [["vox", "46,204,113"], ["bass", "52,152,219"]]) {
      const v = last[key]; if (v < FLO) continue;
      const y = yOf(clamp(v, FLO, FHI)), midi = Math.round(69 + 12 * L2(v / 440));
      ctx.fillStyle = `rgba(${col},1)`; ctx.beginPath(); ctx.arc(W - 9, y, 5, 0, 7); ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.85)"; ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "right";
      ctx.fillText(NOTE_NAMES[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1), W - 18, y + 4); ctx.textAlign = "left";
    }
    ctx.fillStyle = "rgba(46,204,113,0.75)"; ctx.font = "12px ui-monospace, monospace"; ctx.fillText("vocals", W * 0.5 - 50, top + 16);
    ctx.fillStyle = "rgba(52,152,219,0.75)"; ctx.fillText("bass", W * 0.5 + 20, top + 16);
  },

  drawDrumKit(ctx, W, H, f) {
    const x0 = W * 0.26, y0 = H * 0.30, dw = W * 0.50, dh = H * 0.62;
    const parts = this.drumParts();
    const order = ["ride", "crash", "hh", "hat", "toms", "snare", "kick"].filter(p => parts.includes(p));
    for (const id of order) {
      const L = DRUM_LAYOUT[id]; if (!L) continue;
      this.drawPiece(ctx, id, L, x0 + L.x * dw, y0 + L.y * dh, L.r * dh,
        (this.kit[id] && this.kit[id].hit) || 0);
    }
  },

  drawPiece(ctx, id, L, px, py, r, hit) {
    const [cr, cg, cb] = L.col;
    const label = () => {
      ctx.fillStyle = "rgba(255,255,255,0.32)"; ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "center"; ctx.fillText(id, px, py + r + 12); ctx.textAlign = "left";
    };
    if (id === "kick") {
      const rr = r * (1 + hit * 0.12);
      const g = ctx.createRadialGradient(px, py, 0, px, py, rr);
      g.addColorStop(0, `rgba(255,255,255,${0.15 + hit * 0.7})`);
      g.addColorStop(0.55, `rgba(${cr},${cg},${cb},${0.6 + hit * 0.4})`);
      g.addColorStop(1, `rgba(${cr * 0.35 | 0},${cg * 0.35 | 0},${cb * 0.35 | 0},0.95)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, rr, 0, 7); ctx.fill();
      ctx.strokeStyle = `rgba(${cr},${cg},${cb},0.9)`; ctx.lineWidth = 3; ctx.stroke();
      ctx.strokeStyle = `rgba(255,255,255,${0.18 + hit * 0.5})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(px, py, rr * 0.6, 0, 7); ctx.stroke();
      if (hit > 0.05) { ctx.strokeStyle = `rgba(255,255,255,${hit * 0.5})`; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(px, py, rr * (1 + (1 - hit) * 0.4), 0, 7); ctx.stroke(); }
      label(); return;
    }
    if (L.type === "cymbal") {
      ctx.strokeStyle = "rgba(120,130,150,0.4)"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(px, py + r * 2.3); ctx.stroke();
      const ry = r * 0.30 * (1 + hit * 0.4);
      const g = ctx.createRadialGradient(px, py, 0, px, py, r);
      const a = 0.4 + hit * 0.6;
      g.addColorStop(0, `rgba(${cr},${cg},${cb},${a})`);
      g.addColorStop(1, `rgba(${cr},${cg},${cb},${a * 0.35})`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(px, py, r, ry, 0, 0, 7); ctx.fill();
      ctx.strokeStyle = `rgba(${cr},${cg},${cb},${0.6 + hit * 0.4})`; ctx.lineWidth = 1; ctx.stroke();
      if (hit > 0.05) { ctx.strokeStyle = `rgba(255,255,255,${hit * 0.7})`; ctx.lineWidth = 2; ctx.beginPath(); ctx.ellipse(px, py, r * (1 + (1 - hit) * 0.6), ry * (1 + (1 - hit) * 0.6), 0, 0, 7); ctx.stroke(); }
      label(); return;
    }
    // drum (snare / toms): top-head ellipse with a small bounce on hit
    const yy = py - hit * r * 0.12, rr = r * (1 + hit * 0.08);
    ctx.fillStyle = `rgba(${cr * 0.4 | 0},${cg * 0.4 | 0},${cb * 0.4 | 0},0.9)`;
    ctx.fillRect(px - rr, yy, rr * 2, rr * 0.5);
    ctx.beginPath(); ctx.ellipse(px, yy + rr * 0.5, rr, rr * 0.42, 0, 0, Math.PI); ctx.fill();
    const g = ctx.createRadialGradient(px, yy, 0, px, yy, rr);
    g.addColorStop(0, `rgba(255,255,255,${0.2 + hit * 0.6})`);
    g.addColorStop(0.5, `rgba(${cr},${cg},${cb},${0.55 + hit * 0.45})`);
    g.addColorStop(1, `rgba(${cr * 0.6 | 0},${cg * 0.6 | 0},${cb * 0.6 | 0},0.9)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(px, yy, rr, rr * 0.5, 0, 0, 7); ctx.fill();
    ctx.strokeStyle = `rgba(${cr},${cg},${cb},0.8)`; ctx.lineWidth = 2; ctx.stroke();
    if (hit > 0.05) { ctx.strokeStyle = `rgba(255,255,255,${hit * 0.6})`; ctx.lineWidth = 2; ctx.beginPath(); ctx.ellipse(px, yy, rr * (1 + (1 - hit) * 0.5), rr * 0.5 * (1 + (1 - hit) * 0.5), 0, 0, 7); ctx.stroke(); }
    label();
  },

  // quadrant-style vocals: two dots, pitch -> vertical center, energy -> spread,
  // brightness -> horizontal drift, both leaving fading trails.
  drawVocals(ctx, W, H, f) {
    const cx0 = W * 0.5, cy0 = H * 0.17, voiced = this.voxHz > 0, col = "46,204,113";
    // smoothed level (kills jitter)
    this.voxLvlS += (this.voxLvl - this.voxLvlS) * 0.3;
    const lvl = this.voxLvlS;
    // targets
    let tyc = cy0, note = "";
    if (voiced) {
      const lo = Math.log2(110), hi = Math.log2(700);
      tyc = cy0 - (clamp((Math.log2(this.voxHz) - lo) / (hi - lo), 0, 1) - 0.5) * H * 0.24;
      const midi = Math.round(69 + 12 * Math.log2(this.voxHz / 440));
      note = NOTE_NAMES[((midi % 12) + 12) % 12];
    }
    const vb = Transport.bands("vocals"), bright = vb[2] / (vb[0] + vb[1] + vb[2] + 1e-6);
    const tx = cx0 + (bright - 0.5) * W * 0.20;
    // smooth positions toward targets (kills stutter)
    this.voxXs = this.voxXs ? this.voxXs + (tx - this.voxXs) * 0.22 : tx;
    this.voxYs = this.voxYs ? this.voxYs + (tyc - this.voxYs) * 0.22 : tyc;
    const x = this.voxXs, yCenter = this.voxYs;
    const spread = lvl * H * 0.11 + H * 0.02;
    const yt = yCenter - spread, yb = yCenter + spread;
    const r = 9 + lvl * 36 + f.beat * 4, a = voiced ? 0.9 : 0.3;

    this.voxHist.push({ x, yt, yb, on: voiced });
    if (this.voxHist.length > 26) this.voxHist.shift();
    for (const key of ["yt", "yb"]) {
      ctx.strokeStyle = `rgba(${col},0.3)`; ctx.lineWidth = 2.5; ctx.beginPath(); let pen = false;
      for (const h of this.voxHist) { if (!h.on) { pen = false; continue; } pen ? ctx.lineTo(h.x, h[key]) : (ctx.moveTo(h.x, h[key]), pen = true); }
      ctx.stroke();
    }
    ctx.strokeStyle = `rgba(${col},${0.2 + lvl * 0.4})`; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, yt); ctx.lineTo(x, yb); ctx.stroke();
    for (const yy of [yt, yb]) {
      const g = ctx.createRadialGradient(x, yy, 0, x, yy, r * 1.8);
      g.addColorStop(0, `rgba(120,255,180,${a})`); g.addColorStop(1, `rgba(${col},0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, yy, r * 1.8, 0, 7); ctx.fill();
      ctx.fillStyle = `rgba(${col},${Math.min(1, a)})`; ctx.beginPath(); ctx.arc(x, yy, r, 0, 7); ctx.fill();
    }
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(46,204,113,0.6)"; ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("vocals", cx0, H * 0.035);
    if (note) { ctx.fillStyle = "rgba(200,255,220,0.95)"; ctx.font = "16px ui-monospace, monospace"; ctx.fillText(note, x + r + 18, yCenter + 5); }
    ctx.textAlign = "left";
  },

  drawBass(ctx, W, H, f) {
    const b = this.inst("bass"), lv = b ? b.smooth : 0;
    const nx = W * 0.10, nw = W * 0.052, top = H * 0.18, bot = H * 0.58, len = bot - top;
    const STR = [41.2, 55.0, 73.4, 98.0];               // E A D G open strings
    // body — taller rounded shape attached at the neck's base (behind neck)
    const byc = bot + len * 0.34, bw = nw * 1.7, bh = len * 0.44;
    const bodyGlow = `rgba(50,90,140,${0.4 + lv * 0.5})`;
    ctx.fillStyle = `rgba(40,52,72,${0.7 + lv * 0.3})`;
    ctx.beginPath(); ctx.ellipse(nx, byc, bw, bh, 0, 0, 7); ctx.fill();
    ctx.strokeStyle = bodyGlow; ctx.lineWidth = 2; ctx.stroke();
    // headstock + tuning pegs
    ctx.fillStyle = "rgba(42,33,24,0.9)"; ctx.fillRect(nx - nw * 0.7, top - len * 0.09, nw * 1.4, len * 0.09);
    ctx.fillStyle = "rgba(170,170,180,0.6)";
    for (let i = 0; i < 4; i++) { ctx.beginPath(); ctx.arc(nx - nw * 0.45 + i * (nw * 0.9 / 3), top - len * 0.045, 2.5, 0, 7); ctx.fill(); }
    // neck + frets
    ctx.fillStyle = "rgba(48,36,26,0.92)"; ctx.fillRect(nx - nw / 2, top, nw, len);
    ctx.strokeStyle = "rgba(120,100,80,0.5)"; ctx.lineWidth = 1; ctx.strokeRect(nx - nw / 2, top, nw, len);
    ctx.strokeStyle = "rgba(180,180,190,0.22)";
    for (let fr = 1; fr <= 12; fr++) { const y = top + (fr / 12) * len; ctx.beginPath(); ctx.moveTo(nx - nw / 2, y); ctx.lineTo(nx + nw / 2, y); ctx.stroke(); }
    // which string + fret is being played
    let activeStr = -1, fret = 0;
    if (this.bassHz > 30) {
      for (let s = 0; s < 4; s++) { const semis = 12 * Math.log2(this.bassHz / STR[s]); if (semis >= -0.5 && semis < 12.5) { activeStr = s; fret = Math.max(0, Math.round(semis)); } }
      if (activeStr < 0) { if (this.bassHz < STR[0]) { activeStr = 0; fret = 0; } else { activeStr = 3; fret = 12; } }
    }
    // strings (active one vibrates)
    for (let s = 0; s < 4; s++) {
      const sx = nx - nw / 2 + (s + 0.5) / 4 * nw;
      const amp = s === activeStr ? lv * nw * 0.9 * Math.sin(this.clock * 50) : 0;
      ctx.strokeStyle = s === activeStr ? `rgba(120,200,255,${0.6 + lv})` : "rgba(150,170,200,0.3)";
      ctx.lineWidth = s === activeStr ? 2 : 1; ctx.beginPath();
      for (let i = 0; i <= 16; i++) { const yy = top + (i / 16) * len, bend = Math.sin(i / 16 * Math.PI) * amp; i ? ctx.lineTo(sx + bend, yy) : ctx.moveTo(sx + bend, yy); }
      ctx.stroke();
    }
    // played-note marker + name
    if (activeStr >= 0 && this.bassHz > 30) {
      const sx = nx - nw / 2 + (activeStr + 0.5) / 4 * nw, fy = top + (fret / 12) * len, r = 10 + lv * 18;
      const g = ctx.createRadialGradient(sx, fy, 0, sx, fy, r);
      g.addColorStop(0, `rgba(160,220,255,${0.85 + lv * 0.15})`); g.addColorStop(1, "rgba(52,152,219,0)");
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(sx, fy, r, 0, 7); ctx.fill();
      ctx.fillStyle = "#dff0ff"; ctx.beginPath(); ctx.arc(sx, fy, 3.5, 0, 7); ctx.fill();   // solid center
      const midi = Math.round(69 + 12 * Math.log2(this.bassHz / 440));
      ctx.fillStyle = "rgba(180,220,255,0.95)"; ctx.font = "13px ui-monospace, monospace";
      ctx.fillText(NOTE_NAMES[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1), nx + nw, fy + 4);
    }
    ctx.fillStyle = "rgba(52,152,219,0.6)"; ctx.font = "11px ui-monospace, monospace";
    ctx.textAlign = "center"; ctx.fillText("bass", nx, top - 8); ctx.textAlign = "left";
  },

  drawKeyboard(ctx, W, H, f) {
    const top = H * 0.84, h = H * 0.15, left = W * 0.30, wdt = W * 0.66;
    const octaves = 3, whites = octaves * 7, ww = wdt / whites;
    const whitePC = [0, 2, 4, 5, 7, 9, 11], blackPC = [1, 3, 6, 8, 10], blackPos = [0.7, 1.7, 3.7, 4.7, 5.7];
    ctx.fillStyle = "rgba(120,220,200,0.55)"; ctx.font = "11px ui-monospace, monospace"; ctx.fillText("piano / harmony", left, top - 8);
    ctx.fillStyle = "rgba(15,18,26,0.9)"; ctx.fillRect(left, top - 7, wdt, 7);   // back rail
    for (let o = 0; o < octaves; o++) for (let i = 0; i < 7; i++) {
      const x = left + (o * 7 + i) * ww, lit = this.chroma[whitePC[i]];
      const press = lit > 0.18 ? lit : 0, dy = press * 8;
      ctx.fillStyle = "rgba(235,238,245,0.9)"; ctx.fillRect(x + 1, top + dy, ww - 2, h - dy);
      if (press > 0) { ctx.fillStyle = `rgba(120,225,205,${0.25 + press * 0.6})`; ctx.fillRect(x + 1, top + dy, ww - 2, h - dy); }
      ctx.fillStyle = "rgba(0,0,0,0.22)"; ctx.fillRect(x + 1, top + h - 4, ww - 2, 4);
      ctx.strokeStyle = "rgba(0,0,0,0.4)"; ctx.lineWidth = 1; ctx.strokeRect(x + 1, top + dy, ww - 2, h - dy);
    }
    for (let o = 0; o < octaves; o++) for (let j = 0; j < 5; j++) {
      const x = left + (o * 7 + blackPos[j]) * ww, lit = this.chroma[blackPC[j]];
      const press = lit > 0.18 ? lit : 0, dy = press * 6, kh = h * 0.62;
      ctx.fillStyle = press > 0 ? `rgba(60,205,185,${0.5 + press * 0.5})` : "rgba(12,14,20,0.97)";
      ctx.fillRect(x + ww * 0.18, top + dy, ww * 0.64, kh - dy);
      ctx.strokeStyle = "rgba(0,0,0,0.6)"; ctx.lineWidth = 1; ctx.strokeRect(x + ww * 0.18, top + dy, ww * 0.64, kh - dy);
    }
  },
};

Scene.setup();
