"""
Layers A & B — cheap, interpretable per-moment descriptors.

A (local global): mix energy level and its trend (build vs drop) inside the
window. B (per-stem): how loud / busy / bright / present each base stem is.

These reuse the frame-level arrays already computed in interactions.StemAnalysis
(RMS, onset envelope, magnitude spectra), so they add almost no cost. They give
the similarity engine an interpretable backbone alongside the MERT embeddings.
"""

import numpy as np
import librosa

from config import SR, HOP_LENGTH
from interactions import BASE_STEMS, _RMS_ABS_FLOOR

_ONSET_REL_THRESH = 0.3      # onset-strength fraction counted as a hit
_ACTIVE_REL_THRESH = 0.15    # RMS fraction of stem peak counted as "present"
# _RMS_ABS_FLOOR (interactions.py, ~-60 dBFS): the thresholds above are relative
# to the stem's own peak, so a bleed-only near-silent stem would otherwise read
# as fully active with dense onsets. Frames below the floor never count.

# Fixed ordered layout → stable descriptor vector.
FEATURE_NAMES = (
    ["energy_mean", "energy_grad"] +
    [f"{s}_{k}" for s in BASE_STEMS
     for k in ("rms", "onset_density", "centroid", "active_frac")]
)


def _centroid(spec_slice, freqs):
    mag = spec_slice.sum(axis=1)
    return float((freqs * mag).sum() / (mag.sum() + 1e-9))


def moment_descriptors(sa, start_t, end_t) -> dict:
    """sa : interactions.StemAnalysis (holds precomputed frame arrays)."""
    f0, f1 = sa._frames(start_t, end_t)
    out = {}

    # A — mix energy from the sum of per-stem RMS frames
    rms_stack = np.stack([sa.rms[s][f0:f1] for s in BASE_STEMS if s in sa.rms])
    mix_rms = rms_stack.sum(axis=0)
    out["energy_mean"] = float(mix_rms.mean())
    out["energy_grad"] = float(np.polyfit(np.arange(mix_rms.size), mix_rms, 1)[0]
                               if mix_rms.size > 1 else 0.0)

    # B — per-stem descriptors
    any_spec = next(iter(sa.spec.values()), None)
    n_fft = (any_spec.shape[0] - 1) * 2 if any_spec is not None else 2048
    freqs = librosa.fft_frequencies(sr=sa.sr, n_fft=n_fft)
    for s in BASE_STEMS:
        rms = sa.rms.get(s)
        onset = sa.onset.get(s)
        spec = sa.spec.get(s)
        if rms is None:
            out[f"{s}_rms"] = out[f"{s}_onset_density"] = 0.0
            out[f"{s}_centroid"] = out[f"{s}_active_frac"] = 0.0
            continue
        seg = rms[f0:f1]
        out[f"{s}_rms"] = float(seg.mean())
        active_thr = max(sa.peak[s] * _ACTIVE_REL_THRESH, _RMS_ABS_FLOOR)
        out[f"{s}_active_frac"] = float((seg > active_thr).mean())
        if onset is not None:
            o = onset[f0:f1]
            thr = (o.max() + 1e-9) * _ONSET_REL_THRESH
            # onset hits only count on frames whose RMS clears the absolute
            # floor — the onset threshold alone is relative to the window max
            m = min(o.size, seg.size)
            hits = ((o[:m] > thr) & (seg[:m] > _RMS_ABS_FLOOR)).sum()
            out[f"{s}_onset_density"] = float(hits / max(1e-6, (end_t - start_t)))
        else:
            out[f"{s}_onset_density"] = 0.0
        out[f"{s}_centroid"] = (_centroid(spec[:, f0:f1], freqs)
                                if spec is not None else 0.0)
    return out


def moment_vector(sa, start_t, end_t) -> np.ndarray:
    d = moment_descriptors(sa, start_t, end_t)
    return np.array([d[name] for name in FEATURE_NAMES], dtype=np.float32)
