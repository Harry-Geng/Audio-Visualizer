"""
Rebuild a song's features + moment index IN PLACE from its already-separated
stems. Unlike re-ingesting the original file, this does NOT re-run separation
and does NOT create a "<title> (2)" duplicate — it just recomputes the analysis
artifacts for the stems that are already on disk.

Use it when a song has a <id>_stems/ folder but is missing its
<id>_features.json / <id>_moments.npz (e.g. a batch run interrupted between
separation and indexing).

  python rebuild_song.py "Artist - Title"          # one song, by id
  python rebuild_song.py --all-missing             # every song missing its moment index
  python rebuild_song.py --all-missing --dry-run   # just list what would be rebuilt

After a rebuild, re-run compute_galaxy.py / backfill_clap.py and restart the
server to fold the changes into the map / text-search / similarity index.
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

from config import LIBRARY_DIR, SR, HOP_LENGTH, FPS, STEM_NAMES, stem_file
from feature_extractor import extract_all
from feature_writer import write_features
from moment_index import build_song_moments, _load_mono


def _analysis_stems(stems_dir):
    """Base analysis stems present in stems_dir (4 standard + any 6-stem extras)."""
    names = list(STEM_NAMES) + [n for n in ("guitar", "piano") if stem_file(stems_dir, n)]
    out = {}
    for n in names:
        y = _load_mono(stems_dir, n)
        if y is not None:
            out[n] = y
    return out


def _prior_flags(song_id):
    """Reuse the tier flags from an existing features.json if one is present."""
    slug = song_id.lower().replace(" ", "_")
    p = os.path.join(LIBRARY_DIR, f"{slug}_features.json")
    if os.path.exists(p):
        try:
            m = json.load(open(p, encoding="utf-8")).get("meta", {})
            return {k: m.get(k) for k in ("lossy_source", "hq_vocals", "drum_kit", "six_stem")}
        except Exception:
            pass
    return {}


def rebuild(song_id, verbose=True):
    an_dir = os.path.join(LIBRARY_DIR, f"{song_id}_stems")
    if not os.path.isdir(an_dir):
        raise FileNotFoundError(f"no stems dir: {an_dir}")
    analysis = _analysis_stems(an_dir)
    if not analysis:
        raise RuntimeError("no analysis stems found")

    base_names = list(analysis.keys())
    n = min(len(a) for a in analysis.values())
    analysis = {k: v[:n] for k, v in analysis.items()}
    mix = sum(analysis[k] for k in base_names).astype(np.float32)

    feats = extract_all(analysis, mix)
    flags = _prior_flags(song_id)
    feats["meta"] = {
        "filename": f"{song_id}.flac",
        "duration_seconds": round(n / SR, 6),
        "sample_rate": SR, "hop_length": HOP_LENGTH, "fps": round(FPS, 6),
        "n_frames": len(feats["macro"]["energy_envelope"]),
        "processed_at": "",
        "lossy_source": flags.get("lossy_source", False),
        "hq_vocals": flags.get("hq_vocals", os.path.isdir(an_dir + "_hq")),
        "drum_kit": flags.get("drum_kit", stem_file(an_dir, "kick") is not None),
        "six_stem": flags.get("six_stem", stem_file(an_dir, "guitar") is not None),
    }
    # write_features derives the slug from this basename → matches the app's lookup
    write_features(feats, os.path.join(LIBRARY_DIR, f"{song_id}.flac"))
    build_song_moments(song_id, an_dir, feats, verbose=False)
    if verbose:
        print(f"    ok  ({len(feats['macro']['beats'])} beats, {round(n / SR)}s)")


def _missing():
    out = []
    for d in sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_stems"))):
        if not os.path.isdir(d):
            continue
        sid = os.path.basename(d)[: -len("_stems")]
        if not all(stem_file(d, s) for s in STEM_NAMES):
            continue
        if not os.path.exists(os.path.join(LIBRARY_DIR, sid + "_moments.npz")):
            out.append(sid)
    return out


def main():
    ap = argparse.ArgumentParser(description="Rebuild features + moment index in place.")
    ap.add_argument("song_id", nargs="?", help="song id (the part before _stems)")
    ap.add_argument("--all-missing", action="store_true",
                    help="rebuild every song that has stems but no _moments.npz")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.all_missing:
        ids = _missing()
    elif args.song_id:
        ids = [args.song_id]
    else:
        ap.error("give a song id, or use --all-missing")

    print(f"[rebuild] library dir: {LIBRARY_DIR}")
    print(f"[rebuild] {len(ids)} song(s)")
    if args.dry_run:
        for s in ids:
            print("    -", s)
        return

    ok = fail = 0
    for i, sid in enumerate(ids, 1):
        print(f"[{i}/{len(ids)}] {sid}")
        try:
            rebuild(sid)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"    FAILED: {e}")
    print(f"\n[rebuild] done — {ok} ok, {fail} failed")
    if ok:
        print("[rebuild] re-run compute_galaxy.py / backfill_clap.py and restart the "
              "server to fold changes into the map / text-search / similarity index.")


if __name__ == "__main__":
    main()
