"""
Compute the 2-D "galaxy" layout of every moment in the library.

Loads emb_mix from every *_moments.npz (~86k moments x 768-dim MERT), reduces
with PCA -> 64, then runs UMAP twice:
  - 2-D  -> point positions (the map)
  - 3-D  -> point colours (locally-consistent RGB: nearby sounds share hue)

Writes <library>/galaxy.npz:
    xy        (n, 2) float32   normalised to roughly [-1, 1]
    rgb       (n, 3) uint8
    song_idx  (n,)   int32     index into song_ids
    t         (n,)   float32   moment midpoint (seconds)
    dur       (n,)   float32   moment duration (seconds)
    song_ids  (m,)   unicode

  python compute_galaxy.py
"""

import os
import sys
import glob
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np

from config import LIBRARY_DIR


def main():
    files = sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_moments.npz")))
    print(f"[galaxy] library dir: {LIBRARY_DIR}")
    print(f"[galaxy] {len(files)} songs", flush=True)

    song_ids, song_idx, ts, durs, embs = [], [], [], [], []
    for i, f in enumerate(files):
        d = np.load(f)
        song_ids.append(os.path.basename(f)[: -len("_moments.npz")])
        s, e = d["start_t"], d["end_t"]
        embs.append(d["emb_mix"].astype(np.float32))
        song_idx.append(np.full(len(s), i, np.int32))
        ts.append(((s + e) / 2).astype(np.float32))
        durs.append((e - s).astype(np.float32))
        if (i + 1) % 150 == 0:
            print(f"[galaxy] loaded {i + 1}/{len(files)}", flush=True)

    X = np.concatenate(embs)
    del embs
    song_idx = np.concatenate(song_idx)
    ts, durs = np.concatenate(ts), np.concatenate(durs)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    print(f"[galaxy] {X.shape[0]} moments; PCA 768->64 ...", flush=True)

    from sklearn.decomposition import PCA
    Xp = PCA(n_components=64).fit_transform(X).astype(np.float32)
    del X

    import umap
    t0 = time.time()
    print("[galaxy] UMAP 2-D (positions) ...", flush=True)
    xy = umap.UMAP(n_components=2, n_neighbors=15, min_dist=0.05,
                   metric="cosine", verbose=True).fit_transform(Xp)
    print(f"[galaxy] 2-D done in {time.time() - t0:.0f}s", flush=True)

    t0 = time.time()
    print("[galaxy] UMAP 3-D (colours) ...", flush=True)
    xyz = umap.UMAP(n_components=3, n_neighbors=15, min_dist=0.3,
                    metric="cosine", verbose=True).fit_transform(Xp)
    print(f"[galaxy] 3-D done in {time.time() - t0:.0f}s", flush=True)

    # positions: centre, then scale by a high percentile so outliers don't
    # squash the interesting middle into a dot.
    xy = xy - xy.mean(axis=0)
    xy /= np.percentile(np.abs(xy), 99.5) + 1e-9
    # colours: percentile-normalise each channel, keep them bright on black
    lo = np.percentile(xyz, 2, axis=0)
    hi = np.percentile(xyz, 98, axis=0)
    rgb01 = np.clip((xyz - lo) / (hi - lo + 1e-9), 0, 1)
    rgb = (60 + rgb01 * 195).astype(np.uint8)

    out = os.path.join(LIBRARY_DIR, "galaxy.npz")
    np.savez_compressed(out, xy=xy.astype(np.float32), rgb=rgb,
                        song_idx=song_idx, t=ts, dur=durs,
                        song_ids=np.array(song_ids))
    print(f"[galaxy] wrote {out} ({len(ts)} points, {len(song_ids)} songs)")


if __name__ == "__main__":
    main()
