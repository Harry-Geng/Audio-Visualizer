"""
Music Microscope — a local web app for high-resolution visual research of
decomposed audio.

Run:
    python microscope.py            # serve every *_stems song in this folder
    python microscope.py --port 842

Then open http://localhost:8000 in a browser.

Architecture
------------
The browser never downloads whole waveforms. For the time window it is
currently showing, it asks the server for exactly `width` pixel-columns of
peak data. When zoomed out the server returns a min/max envelope (DAW style);
when zoomed in far enough that each column spans only a few samples, it returns
the raw samples — so you can inspect individual waveform cycles at sample
resolution. Spectrograms are recomputed per visible window, so they stay crisp
at any zoom. No third-party web deps: stdlib http.server + a tiny PNG encoder.
"""

import io
import os
import re
import glob
import json
import uuid
import zlib
import struct
import argparse
import threading
import wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import numpy as np
import soundfile as sf
import librosa
from scipy.signal import butter, sosfiltfilt

import ingest
from config import SR, HOP_LENGTH, STEM_NAMES, LIBRARY_DIR, stem_file

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "microscope_static")  # code, ships with the app

# ---------------------------------------------------------------------------
# Track decomposition
# ---------------------------------------------------------------------------

# Sub-band filters mirror feature_extractor.py so the microscope shows exactly
# the components the feature pipeline reasons about.
_SUB_BANDS = {
    "drums": {
        "kick":  butter(4, 120,        btype="lowpass",  fs=SR, output="sos"),
        "snare": butter(4, [180, 1200], btype="bandpass", fs=SR, output="sos"),
        "hat":   butter(4, 7000,       btype="highpass", fs=SR, output="sos"),
    },
    "bass": {
        "sub":  butter(4, 80,         btype="lowpass",  fs=SR, output="sos"),
        "mid":  butter(4, [80, 250],  btype="bandpass", fs=SR, output="sos"),
        "high": butter(4, 250,        btype="highpass", fs=SR, output="sos"),
    },
}

# Base stems (Demucs) and the real drum-kit parts (DrumSep), discovered per song.
BASE_STEMS = ["drums", "bass", "vocals", "other", "guitar", "piano"]
DRUM_PARTS = ["kick", "snare", "toms", "hh", "ride", "crash"]
BASS_REGISTERS = ["sub", "mid", "high"]
LEGACY_DRUM_BANDS = ["kick", "snare", "hat"]


def discover_songs():
    """Return {song_id: stems_dir} for every *_stems folder holding 4 stems."""
    songs = {}
    for name in sorted(os.listdir(LIBRARY_DIR)):
        path = os.path.join(LIBRARY_DIR, name)
        if not (os.path.isdir(path) and name.endswith("_stems")):
            continue
        if all(stem_file(path, s) for s in STEM_NAMES):
            songs[name[: -len("_stems")]] = path
    return songs


def _features_path(song_id):
    slug = song_id.lower().replace(" ", "_")
    p = os.path.join(LIBRARY_DIR, f"{slug}_features.json")
    return p if os.path.exists(p) else None


# original (full-quality) source files, preferred for playback over the
# mono 22 kHz analysis mix.
_ORIG_EXTS = (".flac", ".wav", ".aiff", ".aif", ".mp3", ".m4a", ".aac", ".ogg")


def _original_path(song_id):
    for ext in _ORIG_EXTS:
        p = os.path.join(LIBRARY_DIR, song_id + ext)
        if os.path.exists(p):
            return p
    return None


class Song:
    """Lazily-loaded, in-memory decomposition of one song."""

    def __init__(self, song_id, stems_dir):
        self.id = song_id
        self.stems_dir = stems_dir
        self.hq_dir = stems_dir + "_hq"   # <id>_stems_hq, full-rate stereo (if present)
        self.sr = SR
        self.tracks = {}          # name -> float32 mono
        self.norm = {}            # name -> peak abs amplitude (for display scaling)
        self.layout = []          # per-song row list (id/label/group/depth)
        self.duration = 0.0
        self.n_samples = 0
        self._mix_wav_bytes = None
        self._play_wav = None
        self.orig_path = _original_path(song_id)
        self._lock = threading.Lock()
        self._loaded = False

    def _read_mono(self, name):
        y, _ = sf.read(stem_file(self.stems_dir, name),
                       dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        return np.ascontiguousarray(y, dtype=np.float32)

    def _has(self, name):
        return stem_file(self.stems_dir, name) is not None

    def load(self):
        with self._lock:
            if self._loaded:
                return

            base = [s for s in BASE_STEMS if self._has(s)]
            drum_parts = [p for p in DRUM_PARTS if self._has(p)]   # real DrumSep kit
            stems = {s: self._read_mono(s) for s in base}
            for p in drum_parts:
                stems[p] = self._read_mono(p)

            n = min(len(y) for y in stems.values())
            stems = {k: v[:n] for k, v in stems.items()}

            # mix = sum of base stems only (kit parts are subdivisions of drums)
            self.tracks["mix"] = sum(stems[s] for s in base).astype(np.float32)
            for name, y in stems.items():
                self.tracks[name] = y

            # drum kit: real parts if present, else legacy Butterworth band-split
            if "drums" in base and not drum_parts:
                for child, sos in _SUB_BANDS["drums"].items():
                    self.tracks[child] = sosfiltfilt(sos, stems["drums"]).astype(np.float32)
            # bass registers are always the DSP split (no model for these)
            if "bass" in base:
                for child, sos in _SUB_BANDS["bass"].items():
                    self.tracks[child] = sosfiltfilt(sos, stems["bass"]).astype(np.float32)

            for name, y in self.tracks.items():
                self.norm[name] = float(np.abs(y).max()) + 1e-9

            self.layout = self._build_layout(base, drum_parts)
            self.n_samples = n
            self.duration = n / self.sr
            self._loaded = True

    def _build_layout(self, base, drum_parts):
        rows = [{"id": "mix", "label": "MIX", "group": "mix", "depth": 0}]
        for s in base:
            rows.append({"id": s, "label": s, "group": s, "depth": 0})
            if s == "drums":
                kids = drum_parts if drum_parts else \
                    [k for k in LEGACY_DRUM_BANDS if k in self.tracks]
                for k in kids:
                    rows.append({"id": k, "label": k, "group": "drums", "depth": 1})
            elif s == "bass":
                for k in BASS_REGISTERS:
                    if k in self.tracks:
                        rows.append({"id": k, "label": k, "group": "bass", "depth": 1})
        return rows

    # -- API payloads -------------------------------------------------------

    def meta(self):
        self.load()
        feats = {}
        fp = _features_path(self.id)
        if fp:
            try:
                with open(fp) as f:
                    d = json.load(f)
                macro = d.get("macro", {})
                feats = {
                    "tempo": macro.get("tempo"),
                    "key": macro.get("key"),
                    "mode": macro.get("mode"),
                    "beats": macro.get("beats", []),
                    "sections": macro.get("section_boundaries", []),
                    "section_labels": macro.get("section_labels", []),
                    "phrases": macro.get("phrase_boundary_times", []),
                    "fps": d.get("meta", {}).get("fps"),
                    "pitch": {
                        st: {
                            "hz": d["stems"][st].get("pitch_hz", []),
                            "voiced": d["stems"][st].get("voiced", []),
                        }
                        for st in ("vocals", "other")
                        if st in d.get("stems", {}) and "pitch_hz" in d["stems"][st]
                    },
                }
                lossy = bool(d.get("meta", {}).get("lossy_source"))
                hq_vocals = bool(d.get("meta", {}).get("hq_vocals"))
                drum_kit = bool(d.get("meta", {}).get("drum_kit"))
            except Exception:
                feats, lossy, hq_vocals, drum_kit = {}, False, False, False
        else:
            lossy = hq_vocals = drum_kit = False
        pb = self.playback_info()
        pb["lossy"] = lossy
        pb["hq_vocals"] = hq_vocals
        pb["drum_kit"] = drum_kit
        return {
            "id": self.id,
            "sr": self.sr,
            "duration": self.duration,
            "n_samples": self.n_samples,
            "tracks": self.layout,
            "features": feats,
            "playback": pb,
        }

    def peaks(self, name, start, end, width):
        """min/max envelope (or raw samples when very zoomed in) for [start,end]s."""
        self.load()
        y = self.tracks[name]
        norm = self.norm[name]
        s = max(0, int(round(start * self.sr)))
        e = min(self.n_samples, int(round(end * self.sr)))
        if e <= s:
            return {"mode": "minmax", "min": [], "max": []}
        width = max(1, min(int(width), 4000))
        seg = y[s:e]
        n = seg.size
        spc = n / width  # samples per column

        if spc <= 3.0:
            # raw-sample regime: caller can see individual cycles
            return {
                "mode": "samples",
                "y": np.round(seg / norm, 4).tolist(),
                "t0": s / self.sr,
                "dt": 1.0 / self.sr,
            }

        cols = width
        step = n // cols
        m = step * cols
        block = seg[:m].reshape(cols, step)
        mins = block.min(axis=1) / norm
        maxs = block.max(axis=1) / norm
        return {
            "mode": "minmax",
            "min": np.round(mins, 4).tolist(),
            "max": np.round(maxs, 4).tolist(),
        }

    def spectrogram_png(self, name, start, end, width, height):
        self.load()
        y = self.tracks[name]
        s = max(0, int(round(start * self.sr)))
        e = min(self.n_samples, int(round(end * self.sr)))
        if e - s < 256:
            e = min(self.n_samples, s + 256)
        seg = y[s:e].astype(np.float32)

        # bound STFT cost for very wide windows by pre-decimating
        max_samples = 400_000
        sr = self.sr
        if seg.size > max_samples:
            factor = int(np.ceil(seg.size / max_samples))
            seg = seg[::factor]
            sr = sr / factor

        n_mels = max(32, min(int(height), 256))
        target_cols = max(64, min(int(width), 2000))
        hop = max(64, seg.size // target_cols)
        n_fft = max(512, hop * 2)
        S = librosa.feature.melspectrogram(
            y=seg, sr=int(sr), n_fft=n_fft, hop_length=hop, n_mels=n_mels, power=2.0
        )
        S_db = librosa.power_to_db(S, ref=np.max)
        # normalize to 0..1 over a fixed 80 dB window
        img = np.clip((S_db + 80.0) / 80.0, 0, 1)
        img = img[::-1]  # low freq at bottom
        rgb = _MAGMA[(img * 255).astype(np.uint8)]
        return _encode_png(rgb)

    def playback_wav(self):
        """Full-quality playback audio. Prefers the original source file
        (stereo, native sample rate); falls back to the mono 22 kHz mix."""
        self.load()
        if self._play_wav is not None:
            return self._play_wav

        if self.orig_path:
            y, sr = sf.read(self.orig_path, dtype="float32", always_2d=True)
            channels = y.shape[1]
        else:
            mono = self.tracks["mix"]
            peak = np.abs(mono).max() + 1e-9
            y = (mono / peak * 0.97).reshape(-1, 1)
            sr, channels = self.sr, 1

        pcm16 = (np.clip(y, -1, 1) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes(pcm16.tobytes())
        self._play_wav = buf.getvalue()
        return self._play_wav

    def clip_wav(self, start, end):
        """Small WAV slice for similarity previews. Reads only the needed
        frames straight from disk — never triggers a full song load."""
        src = self.orig_path or stem_file(self.stems_dir, "mix")
        if src is None:
            return None
        with sf.SoundFile(src) as f:
            sr = f.samplerate
            a = max(0, min(int(start * sr), f.frames))
            b = max(a, min(int(end * sr), f.frames))
            f.seek(a)
            y = f.read(b - a, dtype="float32", always_2d=True)
        if not self.orig_path:            # analysis mix is unnormalized mono
            y = y / (np.abs(y).max() + 1e-9) * 0.9
        pcm16 = (np.clip(y, -1, 1) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(y.shape[1])
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes(pcm16.tobytes())
        return buf.getvalue()

    def track_wav(self, name):
        """22 kHz mono WAV of one decomposed component, for solo/mute mixing.
        All components share one scale (the mix peak) so soloed levels are
        realistic and muted-subset sums reconstruct the mix correctly."""
        self.load()
        if name in (None, "", "full"):
            return self.playback_wav()
        if name not in self.tracks:
            return self.playback_wav()
        cache = self.__dict__.setdefault("_track_wavs", {})
        if name in cache:
            return cache[name]
        # prefer full-rate stereo HQ stem if available (drums/bass/vocals/other).
        # WAV is streamed as-is; FLAC is decoded to WAV here (browsers get WAV
        # either way). Result is cached, so the decode is one-time per stem.
        hq = stem_file(self.hq_dir, name)
        if hq is not None:
            if hq.lower().endswith(".wav"):
                with open(hq, "rb") as f:
                    cache[name] = f.read()
            else:
                y, sr = sf.read(hq, dtype="float32", always_2d=False)
                buf = io.BytesIO()
                sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
                cache[name] = buf.getvalue()
            return cache[name]
        scale = self.norm["mix"]
        pcm = np.clip(self.tracks[name] / scale, -1, 1)
        pcm16 = (pcm * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm16.tobytes())
        cache[name] = buf.getvalue()
        return cache[name]

    def playback_info(self):
        self.load()
        if self.orig_path:
            i = sf.info(self.orig_path)
            return {"source": os.path.basename(self.orig_path),
                    "sr": i.samplerate, "channels": i.channels, "full_quality": True}
        return {"source": "analysis mix", "sr": self.sr, "channels": 1, "full_quality": False}


# ---------------------------------------------------------------------------
# Magma-ish colormap + minimal PNG encoder (no Pillow / matplotlib needed)
# ---------------------------------------------------------------------------

def _build_magma():
    stops = np.array([
        [0.001, 0.000, 0.014],
        [0.316, 0.071, 0.485],
        [0.716, 0.215, 0.475],
        [0.988, 0.553, 0.382],
        [0.987, 0.991, 0.750],
    ])
    xs = np.linspace(0, 1, len(stops))
    grid = np.linspace(0, 1, 256)
    lut = np.stack([np.interp(grid, xs, stops[:, c]) for c in range(3)], axis=1)
    return (lut * 255).astype(np.uint8)


_MAGMA = _build_magma()


def _encode_png(rgb):
    """Encode an (H, W, 3) uint8 array as a PNG byte string (stdlib only)."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w, _ = rgb.shape
    # prepend filter-type byte (0) to each scanline
    raw = np.zeros((h, w * 3 + 1), dtype=np.uint8)
    raw[:, 1:] = rgb.reshape(h, w * 3)
    comp = zlib.compress(raw.tobytes(), 6)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_SONGS = {}        # id -> stems_dir
_SONG_CACHE = {}   # id -> Song
_CACHE_LOCK = threading.Lock()


def get_song(song_id):
    with _CACHE_LOCK:
        if song_id not in _SONG_CACHE:
            if song_id not in _SONGS:
                return None
            _SONG_CACHE[song_id] = Song(song_id, _SONGS[song_id])
        return _SONG_CACHE[song_id]


def register_song(song_id, stems_dir):
    """Called by the ingest pipeline when a new song is ready."""
    with _CACHE_LOCK:
        _SONGS[song_id] = stems_dir
        _SONG_CACHE.pop(song_id, None)
    print(f"  + ingested: {song_id}")


# ---------------------------------------------------------------------------
# moment similarity index — loads every *_moments.npz into one in-RAM kNN index
# (~1.3 GB for the full library). Built once in a background thread so the server
# stays responsive; queries are fast weighted-cosine matmuls.
# ---------------------------------------------------------------------------
_MINDEX = None
_MINDEX_STATE = {"status": "idle", "loaded": 0, "total": 0}
_MINDEX_LOCK = threading.Lock()
_MINDEX_SONGROWS = {}      # song_id -> [(row, moment_idx, start_t, end_t)] for seeding

# UI facet name -> query weights over the stored facets
_SIM_FACETS = {
    "mix":     {"emb_mix": 1.0},
    "vocals":  {"emb_vocals": 1.0},
    "bass":    {"emb_bass": 1.0},
    "drums":   {"emb_drums": 1.0},
    "melody":  {"emb_other": 1.0},
    "rhythm":  {"interactions": 1.0},
    "texture": {"descriptors": 1.0},
}


def _build_mindex():
    global _MINDEX, _MINDEX_SONGROWS
    try:
        from moment_index import MomentIndex
        files = sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_moments.npz")))
        _MINDEX_STATE.update(total=len(files), loaded=0, status="building")
        idx = MomentIndex()
        for i, f in enumerate(files):
            try:
                idx.add_file(f)
            except Exception as e:
                print(f"[moments] skip {os.path.basename(f)}: {e}")
            _MINDEX_STATE["loaded"] = i + 1
        idx.finalize()
        songrows = {}
        for r, (sid, mi, s0, s1) in enumerate(idx.rows):
            songrows.setdefault(sid, []).append((r, mi, s0, s1))
        _MINDEX_SONGROWS = songrows
        _MINDEX = idx
        _MINDEX_STATE["status"] = "ready"
        print(f"[moments] similarity index ready: {len(idx.rows)} moments / {len(files)} songs")
    except Exception as e:
        _MINDEX_STATE.update(status="error", error=str(e))
        print(f"[moments] index build failed: {e}")


def _ensure_mindex():
    with _MINDEX_LOCK:
        if _MINDEX_STATE["status"] in ("idle", "error"):
            _MINDEX_STATE.update(status="building", loaded=0)
            threading.Thread(target=_build_mindex, name="mindex", daemon=True).start()
    return _MINDEX


def _seed_moment(song_id, t):
    """Resolve a playback time to that song's moment index (containing, else nearest)."""
    rows = _MINDEX_SONGROWS.get(song_id)
    if not rows:
        return None
    best, bestd = rows[0][1], 1e18
    for (_r, mi, s0, s1) in rows:
        if s0 <= t <= s1:
            return mi
        d = min(abs(t - s0), abs(t - s1))
        if d < bestd:
            bestd, best = d, mi
    return best


_INCOMING = os.path.join(HERE, "_incoming")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])

            if path == "/api/songs":
                return self._json([
                    {"id": sid, "title": sid} for sid in sorted(_SONGS)
                ])

            if path == "/api/song":
                song = get_song(q.get("id", [""])[0])
                if not song:
                    return self._json({"error": "not found"}, 404)
                return self._json(song.meta())

            if path == "/api/peaks":
                song = get_song(q["id"][0])
                if not song:
                    return self._json({"error": "not found"}, 404)
                data = song.peaks(
                    q["track"][0],
                    float(q["start"][0]), float(q["end"][0]), float(q["width"][0]),
                )
                return self._json(data)

            if path == "/api/spectrogram":
                song = get_song(q["id"][0])
                if not song:
                    return self._json({"error": "not found"}, 404)
                png = song.spectrogram_png(
                    q["track"][0],
                    float(q["start"][0]), float(q["end"][0]),
                    float(q["width"][0]), float(q.get("height", ["128"])[0]),
                )
                return self._send(200, png, "image/png")

            if path == "/api/audio":
                song = get_song(q["id"][0])
                if not song:
                    return self._json({"error": "not found"}, 404)
                track = q.get("track", ["full"])[0]
                wav = song.playback_wav() if track in ("full", "") else song.track_wav(track)
                return self._send(200, wav, "audio/wav", {"Accept-Ranges": "none"})

            if path == "/api/clip":
                song = get_song(q["id"][0])
                if not song:
                    return self._json({"error": "not found"}, 404)
                start = max(0.0, float(q.get("start", ["0"])[0]))
                end = float(q.get("end", [str(start + 6.0)])[0])
                end = max(start + 0.25, min(end, start + 20.0))
                wav = song.clip_wav(start, end)
                if wav is None:
                    return self._json({"error": "no audio"}, 404)
                return self._send(200, wav, "audio/wav", {"Accept-Ranges": "none"})

            if path == "/api/lyrics":
                p = os.path.join(LIBRARY_DIR, f"{q['id'][0]}_lyrics.json")
                if not os.path.exists(p):
                    return self._json({"error": "no lyrics"}, 404)
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "application/json")

            if path == "/api/job":
                j = ingest.get_job(q.get("id", [""])[0])
                return self._json(j or {"error": "not found"}, 200 if j else 404)

            if path == "/api/sim_status":
                return self._json(dict(_MINDEX_STATE))

            if path == "/api/similar":
                idx = _ensure_mindex()
                if idx is None:                         # still building → tell the UI
                    return self._json({"status": _MINDEX_STATE["status"],
                                       "loaded": _MINDEX_STATE["loaded"],
                                       "total": _MINDEX_STATE["total"]}, 202)
                sid = q.get("id", [""])[0]
                if sid not in _SONGS:
                    return self._json({"error": "unknown song"}, 404)
                facet = q.get("facet", ["mix"])[0]
                weights = _SIM_FACETS.get(facet, _SIM_FACETS["mix"])
                k = max(1, min(40, int(q.get("k", ["12"])[0])))
                mi = int(q["moment"][0]) if "moment" in q else _seed_moment(sid, float(q.get("t", ["0"])[0]))
                if mi is None:
                    return self._json({"error": "no moments for song"}, 404)
                try:
                    res = idx.query(sid, mi, weights=weights, k=k, exclude_same_song=True)
                except KeyError:
                    return self._json({"error": "moment not in index"}, 404)
                seed = next(((s0, s1) for (_r, m2, s0, s1) in _MINDEX_SONGROWS.get(sid, [])
                             if m2 == mi), (0.0, 0.0))
                for r in res:
                    r["title"] = r["song_id"]
                return self._json({
                    "facet": facet,
                    "seed": {"song_id": sid, "moment_idx": mi,
                             "start_t": seed[0], "end_t": seed[1]},
                    "results": res,
                })

            return self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)
        except BrokenPipeError:
            pass

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        hq = q.get("hq", ["0"])[0] == "1"
        drums = q.get("drums", ["0"])[0] == "1"
        six = q.get("six", ["0"])[0] == "1"
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self._read_exact(length)

            if u.path == "/api/ingest_url":
                data = json.loads(body or b"{}")
                url = (data.get("url") or "").strip()
                if not url:
                    return self._json({"error": "no url"}, 400)
                return self._json(ingest.start_url_job(url, hq_vocals=hq, drum_kit=drums, six_stem=six))

            if u.path == "/api/ingest_file":
                fname = os.path.basename(self.headers.get("X-Filename", "upload"))
                if not body:
                    return self._json({"error": "empty body"}, 400)
                os.makedirs(_INCOMING, exist_ok=True)
                ext = os.path.splitext(fname)[1] or ".bin"
                dest = os.path.join(_INCOMING, uuid.uuid4().hex + ext)
                with open(dest, "wb") as f:
                    f.write(body)
                title = os.path.splitext(fname)[0]
                return self._json(ingest.start_file_job(dest, title, hq_vocals=hq, drum_kit=drums, six_stem=six))

            return self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)
        except BrokenPipeError:
            pass

    def _read_exact(self, n):
        chunks, got = [], 0
        while got < n:
            b = self.rfile.read(min(n - got, 1 << 20))
            if not b:
                break
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)

    do_HEAD = do_GET

    def _serve_static(self, rel):
        rel = rel.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)


def main():
    ap = argparse.ArgumentParser(description="Music Microscope server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    global _SONGS
    _SONGS = discover_songs()
    ingest.on_song_ready = register_song
    if not _SONGS:
        print("No songs yet — drop a file or paste a URL in the browser to add one.")

    print(f"Found {len(_SONGS)} song(s):")
    for sid in sorted(_SONGS):
        print(f"  - {sid}")
    _ensure_mindex()          # start loading the moment-similarity index in the background
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n  Music Microscope → {url}\n  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
