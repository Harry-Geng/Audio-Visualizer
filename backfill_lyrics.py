"""
Backfill lyrics for songs already in the library (which were processed before the
lyrics feature existed). Resumable: skips songs that already have <id>_lyrics.json.

  python backfill_lyrics.py            # all songs missing lyrics
  python backfill_lyrics.py --limit 5  # just a few (e.g. to spot-check)
  python backfill_lyrics.py --force    # rebuild even if lyrics already exist
  python backfill_lyrics.py --no-align # LRCLIB text/line-timing only (skip GPU)
"""

import os
import re
import sys
import json
import glob
import argparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import LIBRARY_DIR, STEM_NAMES, stem_file
import lyrics


def _batchlog_titles():
    """song_id -> (title, yt_duration) from the batch runner's log, if present."""
    out = {}
    p = os.path.join(LIBRARY_DIR, "batch_log.jsonl")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("song_id"):
                out[r["song_id"]] = (r.get("title"), r.get("yt_duration"))
    return out


def _features_duration(song_id):
    for name in (song_id, song_id.lower().replace(" ", "_")):   # verbatim, then legacy slug
        p = os.path.join(LIBRARY_DIR, name + "_features.json")
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8")).get("meta", {}).get("duration_seconds")
            except Exception:
                pass
    return None


def _meta_for(song_id, titles):
    """Return (artist, track, duration) for a song id."""
    title, dur = titles.get(song_id, (None, None))
    title = title or song_id                     # fall back to the id ("Artist - Track")
    dur = _features_duration(song_id) or dur
    artist, track = (title.split(" - ", 1) + [""])[:2] if " - " in title else ("", title)
    return artist.strip(), track.strip(), dur


def _library_songs():
    songs = []
    for d in sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_stems"))):
        if not os.path.isdir(d):
            continue
        sid = os.path.basename(d)[: -len("_stems")]
        if all(stem_file(d, s) for s in STEM_NAMES):
            songs.append((sid, d))
    return songs


def main():
    ap = argparse.ArgumentParser(description="Backfill lyrics for the library.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="rebuild existing lyrics")
    ap.add_argument("--no-align", action="store_true", help="skip word-level alignment")
    args = ap.parse_args()

    titles = _batchlog_titles()
    songs = _library_songs()
    print(f"[lyrics] library dir: {LIBRARY_DIR}")
    print(f"[lyrics] {len(songs)} songs in library")

    done = miss = fail = 0
    for sid, sdir in songs:
        if args.limit and (done + miss + fail) >= args.limit:
            break
        out = os.path.join(LIBRARY_DIR, f"{sid}_lyrics.json")
        if os.path.exists(out) and not args.force:
            continue
        artist, track, dur = _meta_for(sid, titles)
        try:
            data = lyrics.build_song_lyrics(sid, artist, track, dur, sdir,
                                            align=not args.no_align, verbose=True)
            if data.get("source") == "none":
                miss += 1
            else:
                done += 1
        except Exception as e:
            fail += 1
            print(f"  [{sid}] FAILED: {e}")

    print(f"\n[lyrics] done: {done}  no-match: {miss}  failed: {fail}")


if __name__ == "__main__":
    main()
