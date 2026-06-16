"""
Gestural music visualizer — captures musical shape over time.
Usage: python visualizer.py <audio_file>
       Space = pause/resume, scroll bar to seek, volume slider
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
import matplotlib.colors as mcolors
import matplotlib.widgets as mwidgets
from scipy.ndimage import uniform_filter1d
import sounddevice as sd
import soundfile as sf


# ── config ────────────────────────────────────────────────────────────────────

WINDOW_SECS = 12
PLAYHEAD_POS = 0.35

STEMS  = ["vocals", "other", "bass", "drums"]
COLORS = ["#c678dd", "#ffa502", "#2ed573", "#ff4757"]
LABELS = ["Vocals", "Other", "Bass", "Drums"]
BG     = "#060608"

# downsample visual data — keeps fill_between light
# ~6 points per second is plenty for smooth ribbons
VIS_HOP = 7


# ── resolve paths ─────────────────────────────────────────────────────────────

audio_path = sys.argv[1] if len(sys.argv) > 1 else None
if not audio_path:
    print("Usage: python visualizer.py <audio_file>")
    sys.exit(1)

audio_path = os.path.abspath(audio_path)
base       = os.path.splitext(os.path.basename(audio_path))[0]
audio_dir  = os.path.dirname(audio_path)
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
t        = np.arange(n_frames) / fps

beats         = np.array(d["macro"]["beats"])
tempo         = d["macro"]["tempo"]
phrase_times  = np.array(d["macro"]["phrase_boundary_times"])
section_times = np.array(d["macro"]["section_boundaries"])

KEYS    = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
key_str = f"{KEYS[d['macro']['key']]} {'Major' if d['macro']['mode'] else 'Minor'}"


# ── precompute visual curves ──────────────────────────────────────────────────

def norm01(arr):
    mn, mx = arr.min(), arr.max()
    return (arr - mn) / (mx - mn + 1e-10)


stem_viz = {}
for stem in STEMS:
    sd_data   = d["stems"][stem]
    rms       = np.array(sd_data["rms"])
    centroid  = np.array(sd_data["spectral_centroid"])
    dyn_grad  = np.array(sd_data["dynamic_gradient"])
    attack    = np.array(sd_data["attack_envelope"])
    flux      = np.array(sd_data["spectral_flux"])
    onset_str = np.array(sd_data["onset_strength"])

    # y contour
    if stem in ("vocals", "other") and "pitch_hz" in sd_data:
        pitch  = np.array(sd_data["pitch_hz"])
        voiced = np.array(sd_data["voiced"])
        log_p  = np.log2(np.where(pitch > 0, pitch, 1.0) + 1e-10)
        valid  = pitch > 0
        if valid.any():
            contour = (log_p - log_p[valid].min()) / \
                      (log_p[valid].max() - log_p[valid].min() + 1e-10)
        else:
            contour = np.full_like(pitch, 0.5)
        contour = np.where(voiced > 0.5, contour, 0.5)
        contour = uniform_filter1d(contour, size=5)
    else:
        contour = norm01(uniform_filter1d(centroid, size=9))

    contour = (contour - 0.5) * 0.7

    # ribbon half-width
    rms_norm = rms / (rms.max() + 1e-10)
    half_w   = rms_norm * 0.28
    dg_norm  = dyn_grad / (np.abs(dyn_grad).max() + 1e-10)
    half_w  *= (1.0 + np.clip(dg_norm, -0.4, 0.6))
    half_w   = uniform_filter1d(half_w, size=5)

    # staccato points
    at = np.percentile(attack[attack > 0], 85) if (attack > 0).any() else 1.0
    stac_mask = (attack > at) & (onset_str > np.percentile(onset_str, 70))
    stac_idx  = np.where(stac_mask)[0]

    stem_viz[stem] = {
        "contour": contour,
        "half_w": half_w,
        "stac_idx": stac_idx,
    }


# downsample for drawing
t_ds = t[::VIS_HOP]

print("Data ready.")


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
fig, axes = plt.subplots(len(STEMS), 1, figsize=(16, 10))
fig.subplots_adjust(left=0.06, right=0.92, top=0.91, bottom=0.07, hspace=0.12)
fig.patch.set_facecolor(BG)

fig.text(0.5, 0.97, base, ha="center", va="top",
         fontsize=12, color="#dddddd", fontweight="bold")
fig.text(0.5, 0.94, f"{tempo:.0f} BPM  ·  {key_str}",
         ha="center", va="top", fontsize=9, color="#666666")

lane_height = 1.0

for i, (ax, stem, color, label) in enumerate(zip(axes, STEMS, COLORS, LABELS)):
    sv = stem_viz[stem]
    ax.set_facecolor(BG)
    ax.set_ylim(-lane_height / 2, lane_height / 2)
    ax.set_xlim(0, WINDOW_SECS)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    y_top = (sv["contour"] + sv["half_w"])[::VIS_HOP]
    y_bot = (sv["contour"] - sv["half_w"])[::VIS_HOP]
    ctr   = sv["contour"][::VIS_HOP]

    # ribbon body
    ax.fill_between(t_ds, y_bot, y_top, color=color, alpha=0.45, linewidth=0)
    # core contour line
    ax.plot(t_ds, ctr, color=color, linewidth=0.9, alpha=0.75)

    # staccato dots (sparse, no performance issue)
    if len(sv["stac_idx"]) > 0:
        ax.scatter(t[sv["stac_idx"]], sv["contour"][sv["stac_idx"]],
                   s=10, color="white", alpha=0.65, zorder=4, edgecolors="none")

    # beat ticks — small marks at top
    for b in beats:
        ax.plot([b, b], [0.38, 0.46], color="#ffffff", alpha=0.12, linewidth=0.5)

    # phrase boundaries
    for p in phrase_times:
        ax.axvline(p, color="#ffffff", alpha=0.07, linewidth=1.5, linestyle="--")

    # label
    ax.text(0.005, 0.93, label, transform=ax.transAxes,
            fontsize=9, color=color, fontweight="bold", va="top")


# playhead + text
playheads = []
for ax in axes:
    playheads.append(
        ax.axvline(0, color="#ffffff", linewidth=1.8, alpha=0.8, zorder=10))

time_text  = axes[0].text(0.995, 0.93, "0:00", transform=axes[0].transAxes,
                          ha="right", va="top", fontsize=10,
                          color="#555555", fontfamily="monospace")
pause_text = axes[0].text(0.5, 0.5, "", transform=axes[0].transAxes,
                          ha="center", va="center", fontsize=14,
                          color="#ffffff", fontweight="bold", alpha=0.6)


# ── keyboard ──────────────────────────────────────────────────────────────────

fig.canvas.mpl_connect("key_press_event",
                        lambda e: pb.toggle_pause() if e.key == " " else None)


# ── seek bar ──────────────────────────────────────────────────────────────────

ax_seek = fig.add_axes([0.06, 0.025, 0.86, 0.018], facecolor="#1a1a1a")
seek_slider = mwidgets.Slider(
    ax_seek, "", 0, duration, valinit=0,
    color="#555555", track_color="#1a1a1a",
)
seek_slider.valtext.set_visible(False)

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
fig.canvas.mpl_connect("button_press_event", _on_press)
fig.canvas.mpl_connect("button_release_event", _on_release)


# ── volume slider (vertical, right side) ─────────────────────────────────────

ax_vol = fig.add_axes([0.955, 0.25, 0.015, 0.4], facecolor="#1a1a1a")
vol_slider = mwidgets.Slider(
    ax_vol, "", 0, 1, valinit=0.8, orientation="vertical",
    color="#888888", track_color="#1a1a1a",
)
vol_slider.valtext.set_visible(False)
fig.text(0.963, 0.67, "Vol", ha="center", fontsize=7, color="#666666")

vol_slider.on_changed(lambda val: pb.set_volume(val))


# ── animation ─────────────────────────────────────────────────────────────────

def update(_):
    elapsed   = pb.get_elapsed()
    win_left  = elapsed - WINDOW_SECS * PLAYHEAD_POS
    win_right = win_left + WINDOW_SECS
    if win_left < 0:
        win_left, win_right = 0.0, float(WINDOW_SECS)
    if win_right > duration:
        win_right = duration
        win_left  = max(0.0, win_right - WINDOW_SECS)

    for ax in axes:
        ax.set_xlim(win_left, win_right)
    for ph in playheads:
        ph.set_xdata([elapsed, elapsed])

    mins, secs = divmod(int(elapsed), 60)
    time_text.set_text(f"{mins}:{secs:02d}")
    pause_text.set_text("PAUSED" if pb.paused else "")

    if not _seeking[0]:
        seek_slider.set_val(elapsed)


ani = animation.FuncAnimation(
    fig, update, interval=50, blit=False, cache_frame_data=False)


# ── go ────────────────────────────────────────────────────────────────────────

print("Visualizer running — Space to pause/resume.")
pb.start()
plt.show()
