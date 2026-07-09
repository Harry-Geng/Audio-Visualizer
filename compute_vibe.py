"""
Project every moment's CLAP audio embedding onto text-defined mood axes —
the "semantic driver" for the vibe visualizations (liquid / weather / drift).

CLAP's text and audio encoders share one embedding space, so a mood axis can be
defined purely in language: embed prompts for each pole ("dark, murky music" vs
"bright, luminous music"), and score a moment by the difference of its cosine
similarity to the two poles. The result is a slow, per-moment (~4s) timeline of
what the music *feels* like — not how loud it is.

Values are normalized to 0..1 against the whole library's distribution
(5th..95th percentile), so re-run this after adding songs to keep the scale
consistent. Raw scores are stored too, so renormalizing is cheap.

One <id>_vibe.json per song:
    { "axes": ["bright", ...],
      "t":     [moment midpoints, seconds],
      "start_t": [...], "end_t": [...],
      "v":     { axis: [0..1 per moment] },
      "raw":   { axis: [cosine-sim difference per moment] } }

  python compute_vibe.py             # all songs with a _clap.npz
  python compute_vibe.py --force     # recompute even if _vibe.json exists
"""

import os
import sys
import glob
import json
import argparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
import torch

from config import LIBRARY_DIR

MODEL_ID = "laion/clap-htsat-unfused"      # must match backfill_clap.py
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Each axis: (negative-pole prompts, positive-pole prompts). Several phrasings
# per pole are averaged — single prompts are noisy in CLAP space.
AXES = {
    "bright": (
        ["dark, murky, gloomy music",
         "shadowy, dim, brooding sound",
         "a dark and heavy track"],
        ["bright, luminous, radiant music",
         "airy, shimmering, sparkling sound",
         "a bright and glistening track"],
    ),
    "warm": (
        ["cold, icy, metallic music",
         "harsh, sterile, digital sound",
         "a cold clinical electronic track"],
        ["warm, mellow, analog music",
         "soft, cozy, organic sound",
         "a warm soulful track"],
    ),
    "dense": (
        ["sparse, minimal music with lots of empty space",
         "a quiet, stripped-back, skeletal arrangement",
         "a few lonely instruments in silence"],
        ["dense, layered, maximal music",
         "a thick wall of sound with many overlapping layers",
         "a full lush arrangement of many instruments"],
    ),
    "tense": (
        ["calm, relaxed, serene music",
         "peaceful, gentle, easygoing sound",
         "a soothing tranquil track"],
        ["tense, anxious, urgent music",
         "aggressive, driving, relentless sound",
         "a menacing suspenseful track"],
    ),
    "euphoric": (
        ["sad, melancholic, mournful music",
         "a sorrowful heartbroken track",
         "wistful, longing, tearful sound"],
        ["euphoric, joyful, ecstatic music",
         "an uplifting celebratory track",
         "blissful, triumphant, soaring sound"],
    ),
    "vast": (
        ["intimate, close, hushed music",
         "a dry, close-miked bedroom recording",
         "a small quiet room with one performer"],
        ["vast, epic, spacious music",
         "a huge reverberant cinematic soundscape",
         "an enormous stadium-sized wall of echo"],
    ),
}

SMOOTH_W = 1   # +/- neighbors averaged over the moment timeline


@torch.no_grad()
def _axis_vectors():
    """axis -> unit vector (pos_mean - neg_mean, normalized) in CLAP space."""
    from transformers import ClapModel, ClapProcessor
    print(f"[vibe] loading {MODEL_ID} text tower on {DEVICE} ...", flush=True)
    proc = ClapProcessor.from_pretrained(MODEL_ID)
    model = ClapModel.from_pretrained(MODEL_ID).to(DEVICE).eval()

    def embed(prompts):
        inputs = proc(text=prompts, return_tensors="pt", padding=True)
        inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
        out = model.get_text_features(**inputs)
        e = out.pooler_output if hasattr(out, "pooler_output") else out
        e = torch.nn.functional.normalize(e, dim=-1)
        e = torch.nn.functional.normalize(e.mean(dim=0), dim=-1)
        return e.cpu().numpy()

    out = {}
    for name, (neg, pos) in AXES.items():
        # keep both poles; score = sim(pos) - sim(neg) (more stable than one
        # collapsed difference vector when poles aren't antipodal in CLAP space)
        out[name] = (embed(neg), embed(pos))
    return out


def _smooth(x, w):
    if w <= 0 or len(x) < 3:
        return x
    out = np.copy(x)
    for i in range(len(x)):
        a, b = max(0, i - w), min(len(x), i + w + 1)
        out[i] = x[a:b].mean()
    return out


def main():
    ap = argparse.ArgumentParser(description="Compute per-moment vibe axes from CLAP embeddings.")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_clap.npz")))
    if not files:
        print("[vibe] no *_clap.npz found — run backfill_clap.py first")
        return
    axes = _axis_vectors()

    # pass 1: raw scores for every song (also feeds library-wide normalization)
    songs = []
    for f in files:
        sid = os.path.basename(f)[: -len("_clap.npz")]
        d = np.load(f)
        emb = d["emb"]                                   # (n, 512) L2-normalized
        raw = {}
        for name, (vneg, vpos) in axes.items():
            score = emb @ vpos - emb @ vneg              # cosine-sim difference
            raw[name] = _smooth(score.astype(np.float64), SMOOTH_W)
        songs.append((sid, d["start_t"], d["end_t"], raw))

    # library-wide percentile normalization per axis
    lo_hi = {}
    for name in axes:
        allv = np.concatenate([s[3][name] for s in songs])
        lo, hi = np.percentile(allv, 5), np.percentile(allv, 95)
        if hi - lo < 1e-6:
            hi = lo + 1e-6
        lo_hi[name] = (lo, hi)
        print(f"[vibe] axis {name:9s} lib range [{lo:+.4f}, {hi:+.4f}]")

    # pass 2: write per-song JSON
    for sid, st, et, raw in songs:
        outp = os.path.join(LIBRARY_DIR, f"{sid}_vibe.json")
        if os.path.exists(outp) and not args.force:
            pass  # still rewrite: normalization depends on the whole library
        mid = ((st + et) / 2).astype(float)
        doc = {
            "axes": list(axes.keys()),
            "t": [round(x, 3) for x in mid],
            "start_t": [round(float(x), 3) for x in st],
            "end_t": [round(float(x), 3) for x in et],
            "v": {}, "raw": {},
        }
        for name in axes:
            lo, hi = lo_hi[name]
            v = np.clip((raw[name] - lo) / (hi - lo), 0, 1)
            doc["v"][name] = [round(float(x), 4) for x in v]
            doc["raw"][name] = [round(float(x), 5) for x in raw[name]]
        with open(outp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        print(f"[vibe] {sid}: {len(mid)} moments -> {os.path.basename(outp)}")

    print(f"[vibe] done — {len(songs)} songs")


if __name__ == "__main__":
    main()
