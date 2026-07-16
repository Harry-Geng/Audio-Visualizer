"""
Taste profile: cluster every moment's CLAP embedding into "sound families" and
auto-name each family with zero-shot text probes (CLAP text tower — same space).

Writes <library>/taste.json:
  { built_at, n_moments, n_songs, clusters: [
      { id, share, label, alt_labels: [..], probes: {text: cos, ...},
        top_songs: [ {song_id, share} .. ],          # songs most OF this family
        exemplars: [ {song_id, t, end} .. ] } ] }    # closest real moments

  python compute_taste.py            # default k=14
  python compute_taste.py --k 18
"""

import os
import sys
import glob
import json
import time
import argparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np

from config import LIBRARY_DIR

MODEL_ID = "laion/clap-htsat-unfused"

# zero-shot vocabulary — each cluster is named by its closest probes
PROBES = [
    "smooth soulful r&b singing", "breathy intimate female vocals",
    "melancholy emotional ballad", "upbeat feel-good pop song",
    "hard-hitting trap beat with 808 bass", "aggressive rap verse",
    "melodic rap with autotune", "chill lo-fi mellow beat",
    "dark moody atmospheric music", "dreamy ambient synth pads",
    "funky groovy bassline", "disco funk rhythm guitar",
    "energetic dance club music", "slow sensual late-night jam",
    "acoustic guitar unplugged song", "warm piano chords",
    "gospel choir harmonies", "afrobeats percussion groove",
    "reggae dancehall riddim", "latin reggaeton beat",
    "cinematic orchestral strings", "epic dramatic build-up",
    "punchy drums breakbeat", "shimmering bright synth lead",
    "heavily distorted electric guitar", "soft falsetto singing",
    "spoken word interlude", "instrumental section with no vocals",
    "club anthem with heavy kick drum", "sad slow piano ballad",
    "triumphant euphoric drop", "hypnotic repetitive groove",
]


def load_moments():
    embs, meta = [], []
    for f in sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_clap.npz"))):
        sid = os.path.basename(f)[: -len("_clap.npz")]
        try:
            d = np.load(f)
            embs.append(d["emb"].astype(np.float32))
            for s, e in zip(d["start_t"], d["end_t"]):
                meta.append((sid, float(s), float(e)))
        except Exception as ex:
            print(f"[taste] skipping {sid}: {ex}")
    if not embs:
        sys.exit("[taste] no *_clap.npz files — run backfill_clap.py first")
    A = np.concatenate(embs)
    A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-9
    return A, meta


def probe_vectors():
    import torch
    from transformers import ClapTextModelWithProjection, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    model = ClapTextModelWithProjection.from_pretrained(MODEL_ID).eval()
    with torch.no_grad():
        out = model(**tok(PROBES, return_tensors="pt", padding=True)).text_embeds
        out = out / out.norm(dim=-1, keepdim=True)
    return out.numpy().astype(np.float32)


def main():
    ap = argparse.ArgumentParser(description="Build the library taste profile.")
    ap.add_argument("--k", type=int, default=14)
    args = ap.parse_args()

    t0 = time.time()
    A, meta = load_moments()
    n = A.shape[0]
    songs = sorted({m[0] for m in meta})
    print(f"[taste] {n} moments / {len(songs)} songs; clustering k={args.k} ...")

    from sklearn.cluster import MiniBatchKMeans
    km = MiniBatchKMeans(n_clusters=args.k, n_init=10, random_state=7,
                         batch_size=4096).fit(A)
    labels = km.labels_
    cents = km.cluster_centers_ / (np.linalg.norm(km.cluster_centers_, axis=1,
                                                  keepdims=True) + 1e-9)

    print("[taste] naming clusters with CLAP text probes ...")
    P = probe_vectors()                       # (n_probes, 512)
    probe_cos = cents @ P.T                   # (k, n_probes)
    # contrastive naming: raw cosines share a global bias (every cluster ranks
    # the same few probes first) — rank by response RELATIVE to the other
    # clusters so labels describe what makes each family distinctive
    rel = probe_cos - probe_cos.mean(axis=0, keepdims=True)
    taken = set()

    song_idx = {s: i for i, s in enumerate(songs)}
    sid_arr = np.array([song_idx[m[0]] for m in meta], np.int32)

    clusters = []
    for c in range(args.k):
        mask = labels == c
        cnt = int(mask.sum())
        if not cnt:
            continue
        order = np.argsort(-rel[c])
        names = [PROBES[i] for i in order[:3]]
        # keep labels unique across clusters (fall back to the next-best probe)
        primary = next((PROBES[i] for i in order if PROBES[i] not in taken), names[0])
        taken.add(primary)
        names = [primary] + [x for x in names if x != primary][:2]
        # songs whose OWN moments fall most in this cluster (min 20 moments)
        per_song = np.bincount(sid_arr[mask], minlength=len(songs)).astype(np.float32)
        totals = np.bincount(sid_arr, minlength=len(songs)).astype(np.float32)
        frac = np.where(totals >= 20, per_song / np.maximum(totals, 1), 0)
        top = np.argsort(-frac)[:8]
        # exemplar moments: nearest to the centroid
        rows = np.where(mask)[0]
        near = rows[np.argsort(-(A[rows] @ cents[c]))[:6]]
        clusters.append({
            "id": c, "share": round(cnt / n, 4),
            "label": names[0], "alt_labels": names[1:],
            "probes": {PROBES[i]: round(float(probe_cos[c][i]), 4) for i in order[:6]},
            "top_songs": [{"song_id": songs[i], "share": round(float(frac[i]), 3)}
                          for i in top if frac[i] > 0],
            "exemplars": [{"song_id": meta[r][0], "t": round(meta[r][1], 2),
                           "end": round(meta[r][2], 2)} for r in near],
        })
    clusters.sort(key=lambda c: -c["share"])

    out = os.path.join(LIBRARY_DIR, "taste.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"built_at": time.strftime("%Y-%m-%d %H:%M"),
                   "n_moments": n, "n_songs": len(songs),
                   "k": args.k, "clusters": clusters}, f, ensure_ascii=False)
    print(f"[taste] wrote {out} ({len(clusters)} clusters, {time.time()-t0:.0f}s)")
    for c in clusters[:6]:
        print(f"    {c['share']*100:4.1f}%  {c['label']}")


if __name__ == "__main__":
    main()
