"""
Quadrant visualizer — present-moment snapshot of musical shape.
Usage: python visualizer_quadrant.py <audio_file>
       Space = pause/resume

4 stems in a 2x2 grid. Each quadrant shows the current gestural state.
Vocals are represented as 2 dots (top/bottom ribbon perimeter).
Other stems shown as radial/polar shapes driven by frequency + dynamics.
"""

import json
import os
import sys
import time

import numpy as np
import matplotlib
matplotlib.use("macosx")
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.widgets as mwidgets
from matplotlib.patches import Ellipse
from scipy.ndimage import uniform_filter1d
import sounddevice as sd
import soundfile as sf


# ── config ────────────────────────────────────────────────────────────────────

N_POINTS = 64       # points around each polar shape
HISTORY  = 12       # trail length in frames for vocal dots

STEMS      = ["vocals", "other", "drums", "bass"]
COLORS     = ["#c678dd", "#ffa502", "#ff4757", "#2ed573"]
LABELS     = ["Vocals", "Other", "Drums", "Bass"]
QUADRANT   = [(0, 0), (0, 1), (1, 0), (1, 1)]     # row, col
BG         = "#060608"


# ── resolve paths ─────────────────────────────────────────────────────────────

audio_path = sys.argv[1] if len(sys.argv) > 1 else None
if not audio_path:
    print("Usage: python visualizer_quadrant.py <audio_file>")
    sys.exit(1)

audio_path = os.path.abspath(audio_path)
base       = os.path.splitext(os.path.basename(audio_path))[0]
audio_dir  = os.path.dirname(audio_path)
stem_dir   = os.path.join(audio_dir, f"{base}_stems")
json_path  = os.path.join(audio_dir, f"{base.lower().replace(' ', '_')}_features.json")

if not os.path.exists(json_path):
    print(f"Features not found: {json_path}")
    print("Run:  python processor.py <audio_file> --save-stems")
    sys.exit(1)


# ── load features ─────────────────────────────────────────────────────────────

print("Loading features...")
d = json.load(open(json_path))
meta     = d["meta"]
fps      = meta["fps"]
n_frames = meta["n_frames"]
duration = meta["duration_seconds"]

beats = np.array(d["macro"]["beats"])
tempo = d["macro"]["tempo"]
KEYS  = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
key_str = f"{KEYS[d['macro']['key']]} {'Major' if d['macro']['mode'] else 'Minor'}"


def norm01(arr):
    mn, mx = arr.min(), arr.max()
    return (arr - mn) / (mx - mn + 1e-10)


# precompute per-stem arrays
stem_data = {}
for stem in STEMS:
    sd_d = d["stems"][stem]
    rms       = np.array(sd_d["rms"])
    centroid  = np.array(sd_d["spectral_centroid"])
    dyn_grad  = np.array(sd_d["dynamic_gradient"])
    attack    = np.array(sd_d["attack_envelope"])
    flux      = np.array(sd_d["spectral_flux"])
    onset_str = np.array(sd_d["onset_strength"])

    rms_norm      = rms / (rms.max() + 1e-10)
    centroid_norm = norm01(uniform_filter1d(centroid, size=7))
    flux_norm     = norm01(flux)
    attack_norm   = norm01(attack)
    dg_norm       = dyn_grad / (np.abs(dyn_grad).max() + 1e-10)

    entry = {
        "rms": rms_norm,
        "centroid": centroid_norm,
        "flux": flux_norm,
        "attack": attack_norm,
        "dg": dg_norm,
        "onset": onset_str / (onset_str.max() + 1e-10),
    }

    if stem in ("vocals", "other") and "pitch_hz" in sd_d:
        pitch  = np.array(sd_d["pitch_hz"])
        voiced = np.array(sd_d["voiced"])
        log_p  = np.log2(np.where(pitch > 0, pitch, 1.0) + 1e-10)
        valid  = pitch > 0
        if valid.any():
            pn = (log_p - log_p[valid].min()) / (log_p[valid].max() - log_p[valid].min() + 1e-10)
        else:
            pn = np.full_like(pitch, 0.5)
        pn = np.where(voiced > 0.5, pn, 0.5)
        pn = uniform_filter1d(pn, size=5)
        entry["pitch"] = pn
        entry["voiced"] = voiced

    stem_data[stem] = entry


# kick / snare / hat band-split envelopes for the drums quadrant
drum_kit_envs = {}
if "kit" in d["stems"]["drums"]:
    for name in ("kick", "snare", "hat"):
        drum_kit_envs[name] = np.asarray(
            d["stems"]["drums"]["kit"][name]["onset_envelope"], dtype=np.float32
        )

# sub / mid / high register envelopes for the bass quadrant
bass_reg_envs = {}
if "registers" in d["stems"]["bass"]:
    for name in ("sub", "mid", "high"):
        bass_reg_envs[name] = np.asarray(
            d["stems"]["bass"]["registers"][name]["onset_envelope"], dtype=np.float32
        )


# ── load audio ────────────────────────────────────────────────────────────────

print("Loading audio...")
mix, sample_rate = sf.read(audio_path, dtype="float32")
if mix.ndim == 1:
    mix = np.stack([mix, mix], axis=1)


# ── playback ──────────────────────────────────────────────────────────────────

class Playback:
    def __init__(self):
        self.paused = False
        self._audio_pos = 0
        self._elapsed = 0.0
        self._volume = 0.8

    def start(self):
        self._audio_pos = 0
        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=2,
            callback=self._cb, blocksize=2048)
        self._stream.start()

    def _cb(self, outdata, frames, time_info, status):
        if self.paused:
            outdata[:] = 0
            return
        pos   = self._audio_pos
        chunk = mix[pos: pos + frames]
        if len(chunk) < frames:
            outdata[:len(chunk)] = chunk * self._volume
            outdata[len(chunk):] = 0
        else:
            outdata[:] = chunk * self._volume
        self._audio_pos = pos + frames

    def get_elapsed(self):
        if not self.paused:
            self._elapsed = self._audio_pos / sample_rate
        return min(self._elapsed, duration)

    def toggle_pause(self):
        if self.paused:
            self.paused = False
        else:
            self._elapsed = self._audio_pos / sample_rate
            self.paused = True

    def seek(self, t_sec):
        t_sec = max(0.0, min(t_sec, duration))
        self._audio_pos = int(t_sec * sample_rate)
        self._elapsed = t_sec

    def set_volume(self, vol):
        self._volume = max(0.0, min(1.0, vol))

pb = Playback()


# ── figure ────────────────────────────────────────────────────────────────────

plt.style.use("dark_background")
fig = plt.figure(figsize=(12, 12))
fig.patch.set_facecolor(BG)

gs = fig.add_gridspec(2, 2, left=0.05, right=0.95, top=0.90, bottom=0.08,
                      hspace=0.15, wspace=0.15)
axes = [fig.add_subplot(gs[r, c]) for r, c in QUADRANT]

fig.text(0.5, 0.97, base, ha="center", va="top",
         fontsize=12, color="#dddddd", fontweight="bold")
fig.text(0.5, 0.94, f"{tempo:.0f} BPM  ·  {key_str}",
         ha="center", va="top", fontsize=9, color="#666666")

# angles for polar shapes
theta = np.linspace(0, 2 * np.pi, N_POINTS, endpoint=False)
theta_closed = np.append(theta, theta[0])


# ── setup each quadrant ──────────────────────────────────────────────────────

artists = {}
for i, (ax, stem, color, label) in enumerate(zip(axes, STEMS, COLORS, LABELS)):
    ax.set_facecolor(BG)
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    ax.text(0.05, 0.95, label, transform=ax.transAxes,
            fontsize=10, color=color, fontweight="bold", va="top")

    if stem == "vocals":
        # 2 dots: top and bottom of the ribbon perimeter
        dot_top, = ax.plot([], [], "o", color=color, markersize=14, alpha=0.9, zorder=5)
        dot_bot, = ax.plot([], [], "o", color=color, markersize=14, alpha=0.9, zorder=5)
        # trails
        trail_top, = ax.plot([], [], "-", color=color, linewidth=2, alpha=0.3, zorder=3)
        trail_bot, = ax.plot([], [], "-", color=color, linewidth=2, alpha=0.3, zorder=3)
        # connecting line
        conn, = ax.plot([], [], "-", color=color, linewidth=1, alpha=0.4, zorder=2)
        artists[stem] = {
            "dot_top": dot_top, "dot_bot": dot_bot,
            "trail_top": trail_top, "trail_bot": trail_bot,
            "conn": conn,
            "hist_top": [], "hist_bot": [],
        }
    elif stem == "drums" and drum_kit_envs:
        # ellipses laid out like a traditional kit (kick big bottom-centre,
        # snare front-left, hi-hat up-right). each hit spawns a fresh
        # elliptical halo ring into the drum's ripple pool, so older
        # ripples keep expanding while new ones bloom on top.
        kit_layout = [
            ("kick",  (0.00, -0.40), 1.40, 0.65, "#ff4757"),
            ("snare", (-0.35, 0.15), 0.65, 0.55, "#ff8893"),
            ("hat",   (0.55, 0.55), 0.55, 0.18, "#ffcfd5"),
        ]
        HALO_POOL = 6
        drum_kit_artists = {}
        for name, pos, w, h, shade in kit_layout:
            main = Ellipse(pos, width=w, height=h,
                            facecolor=shade, alpha=0.32,
                            edgecolor=shade, linewidth=1.4, zorder=3)
            ax.add_patch(main)
            halos = []
            for _ in range(HALO_POOL):
                halo = Ellipse(pos, width=w, height=h,
                                facecolor="none", edgecolor=shade,
                                linewidth=1.5, alpha=0, zorder=2)
                ax.add_patch(halo)
                halos.append({"artist": halo, "age": 9.0})
            drum_kit_artists[name] = {
                "main": main, "halos": halos,
                "pos": pos, "base_w": w, "base_h": h,
            }
        artists[stem] = drum_kit_artists
    elif stem == "bass" and bass_reg_envs:
        # stacked horizontal ellipses — bass register tower: sub at the
        # bottom (foundation), mid in the middle, high on top. each band
        # pulses + spawns elliptical halo ripples, same pattern as drums.
        bass_layout = [
            ("sub",  (0.00, -0.55), 1.50, 0.40, "#1ba85c"),
            ("mid",  (0.00,  0.00), 1.10, 0.35, "#2ed573"),
            ("high", (0.00,  0.55), 0.75, 0.28, "#7ee2a8"),
        ]
        HALO_POOL = 6
        bass_reg_artists = {}
        for name, pos, w, h, shade in bass_layout:
            main = Ellipse(pos, width=w, height=h,
                            facecolor=shade, alpha=0.32,
                            edgecolor=shade, linewidth=1.4, zorder=3)
            ax.add_patch(main)
            halos = []
            for _ in range(HALO_POOL):
                halo = Ellipse(pos, width=w, height=h,
                                facecolor="none", edgecolor=shade,
                                linewidth=1.5, alpha=0, zorder=2)
                ax.add_patch(halo)
                halos.append({"artist": halo, "age": 9.0})
            bass_reg_artists[name] = {
                "main": main, "halos": halos,
                "pos": pos, "base_w": w, "base_h": h,
            }
        artists[stem] = bass_reg_artists
    else:
        # polar shape
        fill = ax.fill(np.zeros(N_POINTS + 1), np.zeros(N_POINTS + 1),
                        color=color, alpha=0.35, zorder=2)[0]
        line, = ax.plot(np.zeros(N_POINTS + 1), np.zeros(N_POINTS + 1),
                        color=color, linewidth=1.5, alpha=0.8, zorder=3)
        # beat pulse ring
        ring, = ax.plot([], [], "-", color="#ffffff", linewidth=1, alpha=0, zorder=1)
        artists[stem] = {"fill": fill, "line": line, "ring": ring}


# ── center combined blob ──────────────────────────────────────────────────────

ax_center = fig.add_axes([0.39, 0.38, 0.22, 0.22])
ax_center.patch.set_visible(False)
ax_center.set_xlim(-1.3, 1.3)
ax_center.set_ylim(-1.3, 1.3)
ax_center.set_aspect("equal")
ax_center.set_xticks([])
ax_center.set_yticks([])
for sp in ax_center.spines.values():
    sp.set_visible(False)

center_fills = []
for color in COLORS:
    cf = ax_center.fill(np.zeros(N_POINTS + 1), np.zeros(N_POINTS + 1),
                        color=color, alpha=0.18, zorder=2)[0]
    center_fills.append(cf)

center_glow = ax_center.fill(np.zeros(N_POINTS + 1), np.zeros(N_POINTS + 1),
                              color="#ffffff", alpha=0.08, zorder=3)[0]
center_line, = ax_center.plot(np.zeros(N_POINTS + 1), np.zeros(N_POINTS + 1),
                               color="#ffffff", linewidth=1.5, alpha=0.7, zorder=4)


# time + controls
time_text  = fig.text(0.5, 0.065, "0:00", ha="center", fontsize=11,
                      color="#555555", fontfamily="monospace")
pause_text = fig.text(0.5, 0.5, "", ha="center", va="center", fontsize=16,
                      color="#ffffff", fontweight="bold", alpha=0.6)

# seek bar
ax_seek = fig.add_axes([0.05, 0.025, 0.82, 0.018], facecolor="#1a1a1a")
seek_slider = mwidgets.Slider(ax_seek, "", 0, duration, valinit=0,
                               color="#555555", track_color="#1a1a1a")
seek_slider.valtext.set_visible(False)

# volume
ax_vol = fig.add_axes([0.91, 0.025, 0.06, 0.018], facecolor="#1a1a1a")
vol_slider = mwidgets.Slider(ax_vol, "", 0, 1, valinit=0.8,
                              color="#888888", track_color="#1a1a1a")
vol_slider.valtext.set_visible(False)
fig.text(0.895, 0.032, "Vol", ha="right", fontsize=7, color="#666666", va="center")

_seeking = [False]
def _on_seek(val):
    if _seeking[0]:
        pb.seek(val)
def _on_press(event):
    if event.inaxes == ax_seek:
        _seeking[0] = True
        pb.paused = True
def _on_release(event):
    if _seeking[0]:
        _seeking[0] = False
        pb.paused = False

seek_slider.on_changed(_on_seek)
vol_slider.on_changed(lambda v: pb.set_volume(v))
fig.canvas.mpl_connect("button_press_event", _on_press)
fig.canvas.mpl_connect("button_release_event", _on_release)
fig.canvas.mpl_connect("key_press_event",
                        lambda e: pb.toggle_pause() if e.key == " " else None)


# ── beat detection helper ─────────────────────────────────────────────────────

beat_flash = [0.0]

# per-drum pulse state (decays each tick, jumps on hit)
drum_pulse      = {"kick": 0.0, "snare": 0.0, "hat": 0.0}
drum_pulse_prev = {"kick": 0.0, "snare": 0.0, "hat": 0.0}
_last_drum_frame = [-1]

# per-bass-register pulse state
bass_pulse      = {"sub": 0.0, "mid": 0.0, "high": 0.0}
bass_pulse_prev = {"sub": 0.0, "mid": 0.0, "high": 0.0}
_last_bass_frame = [-1]


# ── animation ─────────────────────────────────────────────────────────────────

def update(_):
    elapsed = pb.get_elapsed()
    frame   = min(int(elapsed * fps), n_frames - 1)

    # beat flash
    beat_near = beats[(beats >= elapsed - 0.06) & (beats <= elapsed + 0.02)]
    if len(beat_near) > 0:
        beat_flash[0] = 1.0
    else:
        beat_flash[0] *= 0.85

    for i, stem in enumerate(STEMS):
        sd = stem_data[stem]
        rms_val       = sd["rms"][frame]
        centroid_val  = sd["centroid"][frame]
        flux_val      = sd["flux"][frame]
        attack_val    = sd["attack"][frame]
        dg_val        = sd["dg"][frame]
        onset_val     = sd["onset"][frame]

        art = artists[stem]

        if stem == "vocals":
            # pitch → y position, rms → spread between dots
            pitch_val = sd.get("pitch", np.full(n_frames, 0.5))[frame]
            voiced_val = sd.get("voiced", np.ones(n_frames))[frame]

            # map pitch to y: -0.8 to 0.8
            y_center = (pitch_val - 0.5) * 1.6
            # spread between dots from rms
            spread = rms_val * 0.8 + 0.1
            # x position from centroid (brightness → horizontal)
            x = (centroid_val - 0.5) * 1.2

            y_top = y_center + spread
            y_bot = y_center - spread

            # dot size from dynamics
            size = 8 + rms_val * 20 + beat_flash[0] * 8
            alpha = 0.4 + voiced_val * 0.5

            art["dot_top"].set_data([x], [y_top])
            art["dot_bot"].set_data([x], [y_bot])
            art["dot_top"].set_markersize(size)
            art["dot_bot"].set_markersize(size)
            art["dot_top"].set_alpha(alpha)
            art["dot_bot"].set_alpha(alpha)

            # connection line
            art["conn"].set_data([x, x], [y_bot, y_top])
            art["conn"].set_alpha(0.2 + rms_val * 0.4)

            # trail history
            art["hist_top"].append((x, y_top))
            art["hist_bot"].append((x, y_bot))
            if len(art["hist_top"]) > HISTORY:
                art["hist_top"].pop(0)
                art["hist_bot"].pop(0)

            if len(art["hist_top"]) > 1:
                tx, ty = zip(*art["hist_top"])
                art["trail_top"].set_data(tx, ty)
                bx, by = zip(*art["hist_bot"])
                art["trail_bot"].set_data(bx, by)

        elif stem == "drums" and drum_kit_envs:
            # peek at the band envelopes between previous tick and this one,
            # take the max so quick hits aren't missed at 20fps
            prev_f = _last_drum_frame[0]
            _last_drum_frame[0] = frame
            for name in ("kick", "snare", "hat"):
                env_arr = drum_kit_envs[name]
                if prev_f < 0 or frame <= prev_f or (frame - prev_f) > 8:
                    new_val = float(env_arr[frame])
                else:
                    new_val = float(env_arr[prev_f + 1:frame + 1].max())
                new_val = new_val ** 0.55  # gamma so soft hits still read
                drum_pulse_prev[name] = drum_pulse[name]
                drum_pulse[name] = max(drum_pulse[name] * 0.78, new_val)

                da = art[name]

                # rising edge → spawn a new ripple in the next free slot
                if (drum_pulse[name] > drum_pulse_prev[name] + 0.12
                        and drum_pulse[name] > 0.28):
                    # pick the oldest ripple (largest age) to recycle
                    oldest = max(da["halos"], key=lambda h: h["age"])
                    oldest["age"] = 0.0

                # main ellipse — pulses on hit
                p  = drum_pulse[name]
                bw, bh = da["base_w"], da["base_h"]
                grow = 1.0 + p * 0.32
                da["main"].set_width(bw * grow)
                da["main"].set_height(bh * grow)
                da["main"].set_alpha(0.22 + p * 0.60)

                # advance every ripple in the pool independently — older
                # ripples keep expanding and fading even as new ones spawn
                for h in da["halos"]:
                    h["age"] += 0.06
                    age = h["age"]
                    artist = h["artist"]
                    if age < 1.6:
                        ext = 1.0 + age * 0.55
                        artist.set_width(bw * ext)
                        artist.set_height(bh * ext)
                        artist.set_alpha(max(0.0, 0.55 - age * 0.40))
                    else:
                        artist.set_alpha(0.0)

            continue  # skip the polar branch below

        elif stem == "bass" and bass_reg_envs:
            prev_f = _last_bass_frame[0]
            _last_bass_frame[0] = frame
            for name in ("sub", "mid", "high"):
                env_arr = bass_reg_envs[name]
                if prev_f < 0 or frame <= prev_f or (frame - prev_f) > 8:
                    new_val = float(env_arr[frame])
                else:
                    new_val = float(env_arr[prev_f + 1:frame + 1].max())
                new_val = new_val ** 0.55
                bass_pulse_prev[name] = bass_pulse[name]
                bass_pulse[name] = max(bass_pulse[name] * 0.78, new_val)

                da = art[name]
                if (bass_pulse[name] > bass_pulse_prev[name] + 0.12
                        and bass_pulse[name] > 0.28):
                    oldest = max(da["halos"], key=lambda h: h["age"])
                    oldest["age"] = 0.0

                p  = bass_pulse[name]
                bw, bh = da["base_w"], da["base_h"]
                grow = 1.0 + p * 0.32
                da["main"].set_width(bw * grow)
                da["main"].set_height(bh * grow)
                da["main"].set_alpha(0.22 + p * 0.60)

                for h in da["halos"]:
                    h["age"] += 0.06
                    age = h["age"]
                    artist = h["artist"]
                    if age < 1.6:
                        ext = 1.0 + age * 0.55
                        artist.set_width(bw * ext)
                        artist.set_height(bh * ext)
                        artist.set_alpha(max(0.0, 0.55 - age * 0.40))
                    else:
                        artist.set_alpha(0.0)

            continue  # skip the polar branch below

        else:
            # polar shape: radius driven by frequency-band-like pattern
            # use centroid to shift the shape, rms for size, attack for spikiness
            base_r = 0.15 + rms_val * 0.7

            # create organic variation around the circle
            # low-frequency wobble + higher-frequency texture from attack
            wobble = np.sin(theta * 3 + elapsed * 2) * 0.15 * flux_val
            spikes = np.sin(theta * 8 + elapsed * 5) * 0.2 * attack_val
            texture = np.sin(theta * 13 + elapsed * 3) * 0.08

            r = base_r + wobble + spikes + texture

            # crescendo: expand, decrescendo: contract
            r *= (1 + dg_val * 0.3)

            # beat pulse
            r += beat_flash[0] * 0.15

            r = np.clip(r, 0.05, 1.2)

            # to cartesian
            x = r * np.cos(theta)
            y = r * np.sin(theta)
            x_closed = np.append(x, x[0])
            y_closed = np.append(y, y[0])

            art["fill"].set_xy(np.column_stack([x_closed, y_closed]))
            art["line"].set_data(x_closed, y_closed)

            # beat ring
            if beat_flash[0] > 0.3:
                ring_r = 1.0 + (1 - beat_flash[0]) * 0.5
                rx = ring_r * np.cos(theta_closed)
                ry = ring_r * np.sin(theta_closed)
                art["ring"].set_data(rx, ry)
                art["ring"].set_alpha(beat_flash[0] * 0.3)
            else:
                art["ring"].set_alpha(0)

    # ── center combined blob ──────────────────────────────────────────────────
    r_all = []
    for ci, stem in enumerate(STEMS):
        sdat      = stem_data[stem]
        rms_val   = sdat["rms"][frame]
        flux_val  = sdat["flux"][frame]
        attack_val = sdat["attack"][frame]
        dg_val    = sdat["dg"][frame]

        if stem == "vocals":
            pitch_val  = sdat.get("pitch", np.full(n_frames, 0.5))[frame]
            voiced_val = sdat.get("voiced", np.ones(n_frames))[frame]
            phase      = (pitch_val - 0.5) * np.pi
            r_stem     = 0.15 + rms_val * 0.7
            r_stem     = r_stem + np.sin(theta + phase) * 0.2 * voiced_val
            r_stem     = r_stem + np.sin(theta * 5 + elapsed) * 0.08 * rms_val
        else:
            wobble = np.sin(theta * 3 + elapsed * 2) * 0.15 * flux_val
            spikes = np.sin(theta * 8 + elapsed * 5) * 0.2 * attack_val
            texture = np.sin(theta * 13 + elapsed * 3) * 0.08
            r_stem = 0.15 + rms_val * 0.7 + wobble + spikes + texture

        r_stem = r_stem * (1 + dg_val * 0.3) + beat_flash[0] * 0.15
        r_stem = np.clip(r_stem, 0.05, 1.2)
        r_all.append(r_stem)

        xc = np.append(r_stem * np.cos(theta), r_stem[0] * np.cos(theta[0]))
        yc = np.append(r_stem * np.sin(theta), r_stem[0] * np.sin(theta[0]))
        center_fills[ci].set_xy(np.column_stack([xc, yc]))

    r_avg = np.mean(r_all, axis=0)
    xa = np.append(r_avg * np.cos(theta), r_avg[0] * np.cos(theta[0]))
    ya = np.append(r_avg * np.sin(theta), r_avg[0] * np.sin(theta[0]))
    center_glow.set_xy(np.column_stack([xa, ya]))
    center_line.set_data(xa, ya)

    mins, secs = divmod(int(elapsed), 60)
    time_text.set_text(f"{mins}:{secs:02d}")
    pause_text.set_text("PAUSED" if pb.paused else "")

    if not _seeking[0]:
        seek_slider.set_val(elapsed)

    fig.canvas.draw_idle()


ani = animation.FuncAnimation(
    fig, update, interval=50, blit=False, cache_frame_data=False)


# ── go ────────────────────────────────────────────────────────────────────────

print("Quadrant visualizer running — Space to pause/resume.")
pb.start()
plt.show()
