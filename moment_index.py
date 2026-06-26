"""
Moment index — assemble per-moment facets for a song, persist them, and query
moment-to-moment similarity across a library.

Per song we write `<id>_moments.npz` holding, for every moment:
  - start_t / end_t            (moment time bounds)
  - interactions  [n, Di]      (Layer D)
  - descriptors   [n, Dd]      (Layers A/B)
  - emb_<stem>    [n, 768]      (Layer C, per base stem + 'mix')

Querying combines facets with weights so you can ask different questions:
  - "sounds like this whole moment"  -> emb_mix
  - "find similar bass moments"       -> emb_bass
  - "similar interaction style"       -> interactions
Facets are stored separately precisely so the query can weight them.
"""

import os
import json
import glob

import numpy as np
import soundfile as sf

from config import SR
from interactions import StemAnalysis, BASE_STEMS, FEATURE_NAMES as INT_NAMES
import descriptors as _desc
import embeddings as _emb
from moments import segment_by_beats

EMB_STEMS = ["mix"] + BASE_STEMS          # which embeddings we store per moment


def _load_mono(stems_dir, name):
    p = os.path.join(stems_dir, f"{name}.wav")
    if not os.path.exists(p):
        return None
    y, _ = sf.read(p, dtype="float32", always_2d=False)
    return y if y.ndim == 1 else y.mean(axis=1)


def build_song_moments(song_id, stems_dir, features, out_dir=None, verbose=True):
    """Compute all moment facets for one song and write <id>_moments.npz.

    features : the song's features dict (needs macro.beats + meta.duration_seconds).
    Returns the path written.
    """
    out_dir = out_dir or os.path.dirname(os.path.abspath(stems_dir))
    beats = features["macro"]["beats"]
    duration = features["meta"]["duration_seconds"]
    moments = segment_by_beats(beats, duration)

    stems = {n: _load_mono(stems_dir, n) for n in BASE_STEMS}
    kick = _load_mono(stems_dir, "kick")
    if kick is not None:
        stems["kick"] = kick
    stems = {k: v for k, v in stems.items() if v is not None}

    n = min(len(v) for v in stems.values())
    stems = {k: v[:n] for k, v in stems.items()}
    mix = sum(stems[s] for s in BASE_STEMS if s in stems).astype(np.float32)

    sa = StemAnalysis(stems)
    inter = np.stack([sa.moment_vector(m.start_t, m.end_t) for m in moments])
    descs = np.stack([_desc.moment_vector(sa, m.start_t, m.end_t) for m in moments])
    if verbose:
        print(f"  [{song_id}] {len(moments)} moments | interactions {inter.shape} "
              f"| descriptors {descs.shape}")

    arrays = {
        "start_t": np.array([m.start_t for m in moments], np.float32),
        "end_t": np.array([m.end_t for m in moments], np.float32),
        "interactions": inter.astype(np.float32),
        "descriptors": descs.astype(np.float32),
    }
    for s in EMB_STEMS:
        wave = mix if s == "mix" else stems.get(s)
        if wave is None:
            continue
        arrays[f"emb_{s}"] = _emb.stem_moment_embeddings(wave, moments, sr=SR)
        if verbose:
            print(f"    emb_{s} {arrays[f'emb_{s}'].shape}")

    path = os.path.join(out_dir, f"{song_id}_moments.npz")
    np.savez_compressed(path, **arrays)
    meta = {
        "song_id": song_id, "n_moments": len(moments),
        "interaction_names": list(INT_NAMES),
        "descriptor_names": list(_desc.FEATURE_NAMES),
        "emb_stems": [s for s in EMB_STEMS if f"emb_{s}" in arrays],
        "emb_dim": _emb.EMB_DIM,
    }
    with open(path.replace(".npz", ".json"), "w") as f:
        json.dump(meta, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def _l2(x):
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-9)


def _zscore(x):
    mu, sd = x.mean(axis=0), x.std(axis=0) + 1e-9
    return (x - mu) / sd


class MomentIndex:
    """Load many songs' moment files and run weighted moment-to-moment kNN."""

    def __init__(self):
        self.rows = []                 # (song_id, moment_idx, start_t, end_t)
        self.facets = {}               # name -> stacked matrix (query-normalised)
        self._raw = {}                 # name -> list of per-song arrays (pre-stack)

    @classmethod
    def from_dir(cls, root):
        idx = cls()
        for npz in sorted(glob.glob(os.path.join(root, "*_moments.npz"))):
            idx.add_file(npz)
        idx.finalize()
        return idx

    def add_file(self, npz_path):
        d = np.load(npz_path)
        song_id = os.path.basename(npz_path)[: -len("_moments.npz")]
        n = d["start_t"].shape[0]
        for i in range(n):
            self.rows.append((song_id, i, float(d["start_t"][i]), float(d["end_t"][i])))
        for key in d.files:
            if key in ("start_t", "end_t"):
                continue
            self._raw.setdefault(key, []).append(d[key])

    def finalize(self):
        for name, parts in self._raw.items():
            mat = np.concatenate(parts, axis=0).astype(np.float32)
            # embeddings: cosine via L2. handcrafted facets: z-score then L2.
            mat = _l2(mat) if name.startswith("emb_") else _l2(_zscore(mat))
            self.facets[name] = mat
        self._raw = {}

    def _seed_row(self, song_id, moment_idx):
        for r, (sid, mi, _, _) in enumerate(self.rows):
            if sid == song_id and mi == moment_idx:
                return r
        raise KeyError(f"{song_id}#{moment_idx} not in index")

    def query(self, song_id, moment_idx, weights=None, k=8,
              exclude_same_song=True):
        """Weighted similarity. weights: {facet_name: weight}; defaults to emb_mix.
        Returns list of dicts sorted by score desc."""
        weights = weights or {"emb_mix": 1.0}
        seed = self._seed_row(song_id, moment_idx)
        score = np.zeros(len(self.rows), np.float32)
        wsum = 0.0
        for name, w in weights.items():
            mat = self.facets.get(name)
            if mat is None or w == 0:
                continue
            score += w * (mat @ mat[seed])      # cosine (rows are normalised)
            wsum += abs(w)
        if wsum:
            score /= wsum
        order = np.argsort(-score)
        out = []
        for r in order:
            if r == seed:
                continue
            sid, mi, s0, s1 = self.rows[r]
            if exclude_same_song and sid == song_id:
                continue
            out.append({"song_id": sid, "moment_idx": mi,
                        "start_t": s0, "end_t": s1, "score": float(score[r])})
            if len(out) >= k:
                break
        return out
