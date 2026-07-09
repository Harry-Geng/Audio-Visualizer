"use strict";
// Vibe scenes — three ambience-first renderers driven by the semantic vibe
// timeline (Scene.vibe: CLAP text-axis projections, ~4s moments) instead of
// frame-local levels:
//   liquid  — 1960s oil-projector light show: curl-noise dye advection with
//             ping-pong feedback; stems inject dye, mood picks the palette
//   weather — the mood as an atmosphere: sun height, light temperature, fog,
//             cloud cover and wind all come from the vibe axes
//   drift   — flying through the library's learned timbre space (galaxy.npz);
//             the camera rides the current song's moment path
// Shares Scene's WebGL context/program cache (scene.js); loaded after scene.js
// and app.js — uses their globals (Scene, Galaxy, Transport, S, clamp).

// GLSL helpers pasted into fragment sources (after the precision line)
const VNOISE = `
  float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123); }
  float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
               mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
  }
  float fbm(vec2 p){
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 4; i++){ v += a * noise(p); p = p * 2.03 + 19.19; a *= 0.5; }
    return v;
  }`;

const VibeScenes = {
  has(style) { return style === "liquid" || style === "weather" || style === "drift"; },
  render(style, f) {
    const gl = Scene.glInit();
    if (!gl) return Scene.glNotice("WebGL unavailable — this view needs it");
    if (style === "liquid") this.liquid(gl, f);
    else if (style === "weather") this.weather(gl, f);
    else this.drift(gl, f);
  },

  // h in degrees, s/l 0..1 → [r,g,b] 0..1
  hsl(h, s, l) {
    h = ((h % 360) + 360) % 360;
    const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = l - c / 2;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
      : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return [r + m, g + m, b + m];
  },

  // ============================== LIQUID ==============================
  ADVECT_FS: `precision highp float;
    uniform sampler2D u_prev;
    uniform vec2 u_res;
    uniform float u_time, u_dt, u_turb, u_decay;
    uniform int u_n;
    uniform vec2 u_bpos[8];
    uniform float u_brad[8];
    uniform vec3 u_bcol[8];
    uniform float u_bstr[8];
    ${VNOISE}
    vec2 curl(vec2 p){
      float e = 0.04;
      float nx = fbm(p + vec2(0.0, e)) - fbm(p - vec2(0.0, e));
      float ny = fbm(p + vec2(e, 0.0)) - fbm(p - vec2(e, 0.0));
      return vec2(nx, -ny) / (2.0 * e);
    }
    void main(){
      vec2 uv = gl_FragCoord.xy / u_res;
      vec2 asp = vec2(u_res.x / u_res.y, 1.0);
      vec2 p = uv * asp;
      // two swirl scales: broad folds + fine curls; turbulence scales speed
      vec2 v = curl(p * 1.9 + vec2(0.0, u_time * 0.045)) * 0.6
             + curl(p * 5.3 - vec2(u_time * 0.09, 0.0)) * 0.22;
      v *= (0.05 + u_turb * 0.25);
      vec3 dye = texture2D(u_prev, uv - v * u_dt / asp).rgb * u_decay;
      for (int i = 0; i < 8; i++){
        if (i < u_n) {
          vec2 d = (uv - u_bpos[i]) * asp;
          float g = exp(-dot(d, d) / (u_brad[i] * u_brad[i]));
          dye += u_bcol[i] * (g * u_bstr[i]);
        }
      }
      gl_FragColor = vec4(clamp(dye, 0.0, 1.2), 1.0);
    }`,
  DISPLAY_FS: `precision highp float;
    uniform sampler2D u_tex;
    uniform vec2 u_res, u_px;
    uniform float u_bright;
    void main(){
      vec2 uv = gl_FragCoord.xy / u_res;
      vec3 c = texture2D(u_tex, uv).rgb;
      vec3 b = c;
      b += texture2D(u_tex, uv + vec2(u_px.x, 0.0)).rgb;
      b += texture2D(u_tex, uv - vec2(u_px.x, 0.0)).rgb;
      b += texture2D(u_tex, uv + vec2(0.0, u_px.y)).rgb;
      b += texture2D(u_tex, uv - vec2(0.0, u_px.y)).rgb;
      b *= 0.2;
      c = c * 0.72 + b * b * 1.1;                      // soft self-glow
      float l = dot(c, vec3(0.299, 0.587, 0.114));
      c = mix(vec3(l), c, 1.3);                        // saturate (projector dyes)
      c *= (0.7 + u_bright * 0.55);
      vec2 q = uv - 0.5;
      c *= smoothstep(0.85, 0.30, length(q));          // projector vignette
      gl_FragColor = vec4(c, 1.0);
    }`,
  _lq: null,
  _lqTarget(gl, w, h) {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex, fbo };
  },

  liquid(gl, f) {
    const Pa = Scene.glProgram("lq_advect", this.ADVECT_FS);
    const Pd = Scene.glProgram("lq_display", this.DISPLAY_FS);
    if (!Pa || !Pd) return Scene.glNotice("shader failed — see console");
    const [w, h] = Scene.glShowCanvas();
    const sw = Math.max(64, w >> 1), sh = Math.max(64, h >> 1);   // sim at half res
    let L = this._lq;
    if (!L || L.w !== sw || L.h !== sh) {
      this._lq = L = { w: sw, h: sh, a: this._lqTarget(gl, sw, sh), b: this._lqTarget(gl, sw, sh) };
      for (const t of [L.a, L.b]) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, t.fbo);
        gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      }
    }

    // --- dye injection: stems are the brushes, the vibe picks the paint ---
    const V = Scene.vibe, kit = Scene.kit;
    const hueBase = 215 - V.warm * 185;                     // cold blue → warm amber
    const hue2 = hueBase + (V.euphoric - 0.5) * 90;         // sad → indigo, joyful → pink
    const bassLvl = (Scene.inst("bass") || {}).smooth || 0;
    const drum = Scene.inst("drums");
    const kickE = Math.max((kit.kick && kit.kick.hit) || 0, drum ? Math.min(1, drum.onset * 6) : 0);
    const snareE = (kit.snare && kit.snare.hit) || 0;
    const hatE = Math.max((kit.hh && kit.hh.hit) || 0, (kit.hat && kit.hat.hit) || 0,
                          (kit.ride && kit.ride.hit) || 0, (kit.crash && kit.crash.hit) || 0);
    const otherLvl = (Scene.inst("other") || {}).smooth || 0;
    const blobs = [];
    // bass: the ever-present bed the whole image floats on
    blobs.push({ p: [0.5 + Math.sin(Scene.clock * 0.13) * 0.16, 0.16], r: 0.30,
                 c: this.hsl(hueBase - 12, 0.65, 0.32), s: 0.10 + bassLvl * 0.55 });
    if (kickE > 0.25)
      blobs.push({ p: [0.5 + (Math.random() - 0.5) * 0.10, 0.30], r: 0.14 + kickE * 0.08,
                   c: this.hsl(hueBase, 0.85, 0.52), s: kickE * 1.5 });
    if (snareE > 0.25)
      blobs.push({ p: [0.5 + (Scene.frame % 2 ? 0.18 : -0.18), 0.55], r: 0.075,
                   c: this.hsl(hue2 + 25, 0.75, 0.62), s: snareE });
    if (hatE > 0.3)
      blobs.push({ p: [0.2 + Math.random() * 0.6, 0.78], r: 0.035,
                   c: this.hsl(hue2 + 45, 0.6, 0.75), s: hatE * 0.5 });
    if (Scene.voxLvl > 0.02) {
      const y = Scene.voxHz > 0
        ? 0.38 + clamp((Math.log2(Scene.voxHz) - Math.log2(110)) / (Math.log2(700) - Math.log2(110)), 0, 1) * 0.42
        : 0.6;
      blobs.push({ p: [0.5 + Math.sin(Scene.clock * 0.21) * 0.10, y], r: 0.05 + Scene.voxLvl * 0.09,
                   c: this.hsl(hue2, 0.8, 0.6 + V.bright * 0.15), s: Scene.voxLvl * 2.0 });
    }
    if (otherLvl > 0.02)   // harmony: faint wide wash filling the mid-field
      blobs.push({ p: [0.5, 0.55], r: 0.5, c: this.hsl(hue2 - 20, 0.55, 0.45), s: otherLvl * 0.12 });

    const n = Math.min(8, blobs.length);
    const B = this._lqArrs || (this._lqArrs = {
      pos: new Float32Array(16), rad: new Float32Array(8), col: new Float32Array(24), str: new Float32Array(8),
    });
    for (let i = 0; i < n; i++) {
      B.pos[2 * i] = blobs[i].p[0]; B.pos[2 * i + 1] = blobs[i].p[1];
      B.rad[i] = blobs[i].r;
      B.col[3 * i] = blobs[i].c[0]; B.col[3 * i + 1] = blobs[i].c[1]; B.col[3 * i + 2] = blobs[i].c[2];
      B.str[i] = blobs[i].s;
    }

    // --- advect into the back buffer ---
    gl.bindFramebuffer(gl.FRAMEBUFFER, L.b.fbo);
    gl.viewport(0, 0, sw, sh);
    gl.useProgram(Pa.prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, L.a.tex);
    gl.uniform1i(Pa.u("u_prev"), 0);
    gl.uniform2f(Pa.u("u_res"), sw, sh);
    gl.uniform1f(Pa.u("u_time"), Scene.clock);
    gl.uniform1f(Pa.u("u_dt"), Math.min(Scene.dt, 0.033));
    gl.uniform1f(Pa.u("u_turb"), 0.15 + V.tense * 0.9 + Math.min(1, Scene.flux * 4) * 0.5);
    gl.uniform1f(Pa.u("u_decay"), 0.988);
    gl.uniform1i(Pa.u("u_n"), n);
    gl.uniform2fv(Pa.u("u_bpos[0]"), B.pos);
    gl.uniform1fv(Pa.u("u_brad[0]"), B.rad);
    gl.uniform3fv(Pa.u("u_bcol[0]"), B.col);
    gl.uniform1fv(Pa.u("u_bstr[0]"), B.str);
    Scene.glDrawQuad(Pa);

    // --- display ---
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.useProgram(Pd.prog);
    gl.bindTexture(gl.TEXTURE_2D, L.b.tex);
    gl.uniform1i(Pd.u("u_tex"), 0);
    gl.uniform2f(Pd.u("u_res"), w, h);
    gl.uniform2f(Pd.u("u_px"), 1.5 / sw, 1.5 / sh);
    gl.uniform1f(Pd.u("u_bright"), V.bright);
    Scene.glDrawQuad(Pd);
    const tmp = L.a; L.a = L.b; L.b = tmp;
  },

  // ============================== WEATHER ==============================
  WEATHER_FS: `precision highp float;
    uniform vec2 u_res;
    uniform float u_time, u_bright, u_warm, u_dense, u_tense, u_vast, u_beat, u_low;
    ${VNOISE}
    void main(){
      vec2 uv = gl_FragCoord.xy / u_res;
      float asp = u_res.x / u_res.y;
      float day = u_bright;
      float hor = mix(0.42, 0.22, u_vast);                       // vast → more sky
      vec2 sun = vec2(0.5 + 0.28 * sin(u_time * 0.008), mix(hor - 0.05, 0.82, day));
      vec3 lightCol = mix(vec3(0.62, 0.76, 1.0), vec3(1.0, 0.72, 0.42), u_warm);

      vec3 zenith = mix(vec3(0.008, 0.010, 0.026), vec3(0.18, 0.36, 0.64), day);
      vec3 horizonC = mix(vec3(0.03, 0.03, 0.06), lightCol * 0.85, day * 0.85);
      horizonC = mix(horizonC, lightCol * vec3(1.0, 0.62, 0.42), (1.0 - day) * u_warm * 0.65); // sunset band
      vec3 col = mix(horizonC, zenith, smoothstep(hor, 1.0, uv.y));

      vec2 sd = (uv - sun) * vec2(asp, 1.0);
      float dsun = length(sd);
      col += lightCol * (0.10 / (dsun * 8.0 + 0.12)) * (0.35 + day);   // halo
      col += lightCol * smoothstep(0.035, 0.012, dsun) * (0.6 + day);  // disc

      // stars fade in as the mood darkens
      float st = step(0.9974, hash(floor(uv * vec2(asp, 1.0) * 420.0)));
      float tw = 0.5 + 0.5 * sin(u_time * 2.6 + hash(floor(uv * 420.0)) * 40.0);
      col += vec3(st * tw) * (1.0 - day) * 0.4 * smoothstep(hor + 0.04, hor + 0.25, uv.y);

      // clouds: coverage from density, wind from tension, scale from vastness
      float wind = 0.008 + u_tense * 0.055;
      float scale = mix(3.0, 1.6, u_vast);
      float cA = fbm(vec2(uv.x * asp * scale + u_time * wind, uv.y * scale * 2.2 + 7.0));
      float cB = fbm(vec2(uv.x * asp * scale * 2.3 - u_time * wind * 1.7, uv.y * scale * 4.5 + 31.0));
      float cov = mix(0.72, 0.30, u_dense);
      float cloud = smoothstep(cov, cov + 0.28, cA * 0.65 + cB * 0.35)
                  * smoothstep(hor - 0.02, hor + 0.15, uv.y);
      float lit = clamp(1.0 - dsun * 1.4, 0.0, 1.0);
      vec3 cloudCol = mix(zenith * 0.9 + 0.04, lightCol * (0.55 + 0.45 * day), 0.25 + lit * 0.6);
      cloudCol = mix(cloudCol, vec3(0.05, 0.05, 0.09), (1.0 - day) * 0.7);
      col = mix(col, cloudCol, cloud * (0.5 + u_dense * 0.4));

      // ground silhouette + valley fog
      float ridge = hor + (fbm(vec2(uv.x * asp * 2.2 + 3.7, 0.5)) - 0.5) * 0.09 * (1.0 - u_vast * 0.5);
      if (uv.y < ridge) {
        vec3 ground = mix(vec3(0.006, 0.007, 0.012), lightCol * 0.08, day * 0.5);
        float fog = exp(-(ridge - uv.y) * mix(9.0, 2.5, u_dense));
        col = mix(ground, horizonC * 0.9, fog * 0.85);
      }
      // low-end energy glows along the horizon; the beat is a breath, not a strobe
      col += lightCol * u_low * 0.13 * exp(-abs(uv.y - hor) * 8.0);
      col *= 1.0 + u_beat * 0.035;
      vec2 q = uv - 0.5;
      col *= 1.0 - dot(q, q) * 0.55;
      col += (hash(uv * u_res + fract(u_time) * 61.0) * 2.0 - 1.0) * 0.012;  // film grain
      gl_FragColor = vec4(col, 1.0);
    }`,

  weather(gl, f) {
    const P = Scene.glProgram("weather", this.WEATHER_FS);
    if (!P) return Scene.glNotice("shader failed — see console");
    const [w, h] = Scene.glShowCanvas();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    const V = Scene.vibe;
    const bassLvl = (Scene.inst("bass") || {}).smooth || 0;
    const kickE = (Scene.kit.kick && Scene.kit.kick.hit) || 0;
    gl.useProgram(P.prog);
    gl.uniform2f(P.u("u_res"), w, h);
    gl.uniform1f(P.u("u_time"), Scene.clock);
    gl.uniform1f(P.u("u_bright"), V.bright);
    gl.uniform1f(P.u("u_warm"), V.warm);
    gl.uniform1f(P.u("u_dense"), V.dense);
    gl.uniform1f(P.u("u_tense"), V.tense);
    gl.uniform1f(P.u("u_vast"), V.vast);
    gl.uniform1f(P.u("u_beat"), f.beat);
    gl.uniform1f(P.u("u_low"), Math.min(1, bassLvl * 1.6 + kickE * 0.5));
    Scene.glDrawQuad(P);
  },

  // ============================== DRIFT ==============================
  DRIFT_VS: `attribute vec2 a_pos;
    attribute vec3 a_col;
    uniform vec2 u_cam, u_rot, u_scale;
    uniform float u_psize;
    varying vec3 v_col;
    varying float v_near;
    void main(){
      vec2 d = a_pos - u_cam;
      d = vec2(d.x * u_rot.x - d.y * u_rot.y, d.x * u_rot.y + d.y * u_rot.x);
      gl_Position = vec4(d * u_scale, 0.0, 1.0);
      v_near = exp(-length(d) * 3.0);
      v_col = a_col;
      gl_PointSize = u_psize * (0.5 + v_near * 2.4);
    }`,
  DRIFT_PT_FS: `precision mediump float;
    varying vec3 v_col;
    varying float v_near;
    void main(){
      float r = length(gl_PointCoord - 0.5);
      if (r > 0.5) discard;
      float a = smoothstep(0.5, 0.05, r) * (0.16 + v_near * 0.84);
      gl_FragColor = vec4(v_col, a);
    }`,
  DRIFT_LN_VS: `attribute vec2 a_pos;
    uniform vec2 u_cam, u_rot, u_scale;
    void main(){
      vec2 d = a_pos - u_cam;
      d = vec2(d.x * u_rot.x - d.y * u_rot.y, d.x * u_rot.y + d.y * u_rot.x);
      gl_Position = vec4(d * u_scale, 0.0, 1.0);
    }`,
  DRIFT_LN_FS: `precision mediump float;
    uniform vec4 u_col;
    void main(){ gl_FragColor = u_col; }`,
  _dr: null,

  drift(gl, f) {
    const [w, h] = Scene.glShowCanvas();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    const V = Scene.vibe;
    gl.clearColor(0.012 + V.warm * 0.01, 0.012, 0.030 + (1 - V.warm) * 0.015, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (typeof Galaxy === "undefined") return;
    if (!Galaxy._loaded) { if (!Galaxy._loading) Galaxy.load(); return; }

    const Pp = Scene.glProgram("dr_pts", this.DRIFT_PT_FS, this.DRIFT_VS);
    const Pl = Scene.glProgram("dr_line", this.DRIFT_LN_FS, this.DRIFT_LN_VS);
    if (!Pp || !Pl) return Scene.glNotice("shader failed — see console");

    let D = this._dr;
    if (!D || D.n !== Galaxy.n) {
      const n = Galaxy.n;
      const rgb = new Uint8Array(Galaxy.xy.buffer, 20 * n, 3 * n);   // same blob as Galaxy's arrays
      const pos = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, pos);
      gl.bufferData(gl.ARRAY_BUFFER, Galaxy.xy, gl.STATIC_DRAW);
      const col = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, col);
      gl.bufferData(gl.ARRAY_BUFFER, rgb, gl.STATIC_DRAW);
      const head = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, head);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(2), gl.DYNAMIC_DRAW);
      // map center for the no-song fallback
      let cx = 0, cy = 0;
      for (let i = 0; i < n; i++) { cx += Galaxy.xy[2 * i]; cy += Galaxy.xy[2 * i + 1]; }
      this._dr = D = { n, pos, col, head, cam: [cx / n, cy / n], center: [cx / n, cy / n],
                       zoom: 2.0, ang: 0 };
    }

    // camera target: interpolate along the current song's moment path
    let target = D.center, sid = Galaxy.ids.indexOf(S.songId), range = null;
    if (sid >= 0 && Galaxy._ranges.has(sid)) {
      range = Galaxy._ranges.get(sid);
      const [first, count] = range, t = f.t;
      let lo = first, hi = first + count - 1;
      while (lo < hi) { const m = (lo + hi) >> 1; if (Galaxy.t[m] < t) lo = m + 1; else hi = m; }
      const i1 = Math.min(lo, first + count - 1), i0 = Math.max(first, i1 - 1);
      const t0 = Galaxy.t[i0], t1 = Galaxy.t[i1];
      const k = t1 > t0 ? clamp((t - t0) / (t1 - t0), 0, 1) : 0;
      target = [Galaxy.xy[2 * i0] + (Galaxy.xy[2 * i1] - Galaxy.xy[2 * i0]) * k,
                Galaxy.xy[2 * i0 + 1] + (Galaxy.xy[2 * i1 + 1] - Galaxy.xy[2 * i0 + 1]) * k];
    }
    D.cam[0] += (target[0] - D.cam[0]) * 0.05;
    D.cam[1] += (target[1] - D.cam[1]) * 0.05;
    D.ang += Scene.dt * (0.015 + V.tense * 0.04);
    const zoomT = (2.6 - V.vast * 1.1) * (1 + f.beat * 0.02);    // vast mood → pull back
    D.zoom += (zoomT - D.zoom) * 0.03;
    const rot = [Math.cos(D.ang), Math.sin(D.ang)];
    const scale = [D.zoom * h / w, D.zoom];

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);                          // additive starlight
    const aPos = Pp.a("a_pos"), aCol = Pp.a("a_col");

    // all library moments as depth-faded starfield
    gl.useProgram(Pp.prog);
    gl.uniform2f(Pp.u("u_cam"), D.cam[0], D.cam[1]);
    gl.uniform2f(Pp.u("u_rot"), rot[0], rot[1]);
    gl.uniform2f(Pp.u("u_scale"), scale[0], scale[1]);
    gl.uniform1f(Pp.u("u_psize"), Math.max(3, h * 0.006));
    gl.bindBuffer(gl.ARRAY_BUFFER, D.pos);
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, D.col);
    gl.enableVertexAttribArray(aCol);
    gl.vertexAttribPointer(aCol, 3, gl.UNSIGNED_BYTE, true, 0, 0);
    gl.drawArrays(gl.POINTS, 0, D.n);

    // the current song's path through the space
    if (range) {
      gl.useProgram(Pl.prog);
      gl.uniform2f(Pl.u("u_cam"), D.cam[0], D.cam[1]);
      gl.uniform2f(Pl.u("u_rot"), rot[0], rot[1]);
      gl.uniform2f(Pl.u("u_scale"), scale[0], scale[1]);
      gl.uniform4f(Pl.u("u_col"), 1, 1, 1, 0.22);
      const aLn = Pl.a("a_pos");
      gl.bindBuffer(gl.ARRAY_BUFFER, D.pos);
      gl.enableVertexAttribArray(aLn);
      gl.vertexAttribPointer(aLn, 2, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINE_STRIP, range[0], range[1]);

      // glowing "you are here" — pulses with the mix level
      let lvl = 0; for (const o of Scene.insts) lvl = Math.max(lvl, o.smooth);
      gl.useProgram(Pp.prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, D.head);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(target), gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
      gl.disableVertexAttribArray(aCol);
      gl.vertexAttrib3f(aCol, 1.0, 0.95, 0.85);
      gl.uniform1f(Pp.u("u_psize"), (h * 0.02) * (0.8 + lvl * 1.2 + f.beat * 0.25));
      gl.drawArrays(gl.POINTS, 0, 1);
    }
    gl.disableVertexAttribArray(aPos);
    gl.disableVertexAttribArray(aCol);
    gl.disable(gl.BLEND);
  },
};
