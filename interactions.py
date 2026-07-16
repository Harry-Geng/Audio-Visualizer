"""
Layer D — stem-interaction features, computed per moment.

This is the novel core of the similarity engine: instead of asking only
"what does this moment sound like", it measures *how the stems relate* inside
the window — do bass and kick lock together, do vocals duck the instruments,
how dense is the arrangement, do bass and harmony agree on the key.

We precompute frame-level features for each stem once (RMS, onset envelope,
chroma, magnitude spectrum at the project's 512-hop / 22 kHz grid), then every
moment just slices a frame range — correct, edge-effect-free, and cheap enough
for the full library batch.
"""

import numpy as np
import librosa
from scipy.signal import butter, sosfiltfilt

from config import SR, HOP_LENGTH

BASE_STEMS = ["drums", "bass", "vocals", "other"]

# Energy-coupling pairs (RMS-envelope correlation).
_COUPLE_PAIRS = [
    ("drums", "bass"), ("drums", "vocals"), ("bass", "vocals"),
    ("vocals", "other"), ("drums", "other"), ("bass", "other"),
]
# Spectral-masking pairs (who competes for the same frequency space).
_MASK_PAIRS = [("vocals", "other"), ("bass", "drums"), ("vocals", "bass")]

# Fixed, ordered feature names → guarantees a stable interaction vector layout.
FEATURE_NAMES = (
    [f"couple_{a}_{b}" for a, b in _COUPLE_PAIRS] +
    ["groove_kickbass_peak", "groove_kickbass_lag"] +
    [f"mask_{a}_{b}" for a, b in _MASK_PAIRS] +
    ["harm_bass_other", "harm_vocals_other"] +
    ["density_mean", "density_std"] +
    [f"share_{s}" for s in BASE_STEMS]
)

_KICK_SOS = butter(4, 120, btype="lowpass", fs=SR, output="sos")
_GROOVE_MAX_LAG = 4          # frames (~93 ms) to search for kick/bass alignment
_DENSITY_REL_THRESH = 0.15   # stem "active" if RMS > this fraction of its own peak
# Absolute RMS floor (~-60 dBFS; stems are float in [-1, 1]) below which a stem
# frame never counts as active — a bleed-only near-silent stem otherwise reads
# as fully active because the thresholds above are relative to its own peak.
_RMS_ABS_FLOOR = 1e-3


def _safe_corr(a, b):
    """Pearson correlation, 0 when either side is flat/silent."""
    if a.size < 2 or b.size < 2:
        return 0.0
    sa, sb = a.std(), b.std()
    if sa < 1e-9 or sb < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


class StemAnalysis:
    """Frame-level analysis of one song's stems, sliceable per moment."""

    def __init__(self, stems: dict, sr=SR):
        """stems : {name -> mono float32}. Must include the base 4; may include
        a real 'kick' stem (DrumSep) which is preferred over a band-split proxy."""
        self.sr = sr
        self.stems = stems
        self.rms = {}
        self.onset = {}
        self.chroma = {}
        self.spec = {}        # mean-normalisable magnitude spectrum per frame
        self.peak = {}        # per-stem RMS peak, for the density threshold

        for name in BASE_STEMS:
            y = stems.get(name)
            if y is None:
                continue
            self.rms[name] = librosa.feature.rms(y=y, hop_length=HOP_LENGTH)[0]
            self.onset[name] = librosa.onset.onset_strength(
                y=y, sr=sr, hop_length=HOP_LENGTH)
            self.peak[name] = float(self.rms[name].max()) + 1e-9

        # harmonic stems: chroma for key-agreement
        for name in ("bass", "other", "vocals"):
            y = stems.get(name)
            if y is not None:
                self.chroma[name] = librosa.feature.chroma_cqt(
                    y=y, sr=sr, hop_length=HOP_LENGTH)

        # magnitude spectra for masking
        for name in ("vocals", "other", "bass", "drums"):
            y = stems.get(name)
            if y is not None:
                self.spec[name] = np.abs(librosa.stft(y, hop_length=HOP_LENGTH))

        # kick stream for groove lock: real DrumSep kick if present, else
        # low-pass of the drums stem (kick dominates sub-120 Hz).
        kick = stems.get("kick")
        if kick is None and stems.get("drums") is not None:
            kick = sosfiltfilt(_KICK_SOS, stems["drums"]).astype(np.float32)
        self.onset["kick"] = (
            librosa.onset.onset_strength(y=kick, sr=sr, hop_length=HOP_LENGTH)
            if kick is not None else None)

        self.n_frames = min((v.shape[-1] for v in self.rms.values()), default=0)

    def _frames(self, start_t, end_t):
        f0 = max(0, int(start_t * self.sr / HOP_LENGTH))
        f1 = min(self.n_frames, int(end_t * self.sr / HOP_LENGTH))
        return f0, max(f0 + 1, f1)

    def _groove(self, f0, f1):
        k, b = self.onset.get("kick"), self.onset.get("bass")
        if k is None or b is None:
            return 0.0, 0.0
        ks, bs = k[f0:f1], b[f0:f1]
        if ks.size < 3 or bs.size < 3:
            return 0.0, 0.0
        best, best_lag = 0.0, 0
        for lag in range(-_GROOVE_MAX_LAG, _GROOVE_MAX_LAG + 1):
            if lag < 0:
                c = _safe_corr(ks[-lag:], bs[:lag] if lag else bs)
            elif lag > 0:
                c = _safe_corr(ks[:-lag], bs[lag:])
            else:
                c = _safe_corr(ks, bs)
            if c > best:
                best, best_lag = c, lag
        return best, best_lag / _GROOVE_MAX_LAG   # normalise lag to [-1, 1]

    def _mask(self, name_a, name_b, f0, f1):
        sa, sb = self.spec.get(name_a), self.spec.get(name_b)
        if sa is None or sb is None:
            return 0.0
        pa = sa[:, f0:f1].mean(axis=1)
        pb = sb[:, f0:f1].mean(axis=1)
        pa = pa / (pa.sum() + 1e-9)
        pb = pb / (pb.sum() + 1e-9)
        return float(np.minimum(pa, pb).sum())   # histogram intersection 0..1

    def _harm(self, name_a, name_b, f0, f1):
        ca, cb = self.chroma.get(name_a), self.chroma.get(name_b)
        if ca is None or cb is None:
            return 0.0
        return _safe_corr(ca[:, f0:f1].mean(axis=1), cb[:, f0:f1].mean(axis=1))

    def _density(self, f0, f1):
        active = []          # per-frame count of active base stems
        shares = {}          # per-stem mean energy in the window
        present = [s for s in BASE_STEMS if s in self.rms]
        mat = np.stack([self.rms[s][f0:f1] for s in present])      # [stems, frames]
        thr = np.array([[max(self.peak[s] * _DENSITY_REL_THRESH, _RMS_ABS_FLOOR)]
                        for s in present])
        active = (mat > thr).sum(axis=0).astype(np.float32)
        total = mat.sum(axis=0) + 1e-9
        share = (mat.mean(axis=1) / (mat.mean(axis=1).sum() + 1e-9))
        for s, v in zip(present, share):
            shares[s] = float(v)
        return float(active.mean()), float(active.std()), shares

    def moment_features(self, start_t, end_t) -> dict:
        f0, f1 = self._frames(start_t, end_t)
        out = {}
        for a, b in _COUPLE_PAIRS:
            ra, rb = self.rms.get(a), self.rms.get(b)
            out[f"couple_{a}_{b}"] = (
                _safe_corr(ra[f0:f1], rb[f0:f1]) if ra is not None and rb is not None else 0.0)
        peak, lag = self._groove(f0, f1)
        out["groove_kickbass_peak"], out["groove_kickbass_lag"] = peak, lag
        for a, b in _MASK_PAIRS:
            out[f"mask_{a}_{b}"] = self._mask(a, b, f0, f1)
        out["harm_bass_other"] = self._harm("bass", "other", f0, f1)
        out["harm_vocals_other"] = self._harm("vocals", "other", f0, f1)
        dmean, dstd, shares = self._density(f0, f1)
        out["density_mean"], out["density_std"] = dmean, dstd
        for s in BASE_STEMS:
            out[f"share_{s}"] = shares.get(s, 0.0)
        return out

    def moment_vector(self, start_t, end_t) -> np.ndarray:
        f = self.moment_features(start_t, end_t)
        return np.array([f[name] for name in FEATURE_NAMES], dtype=np.float32)
