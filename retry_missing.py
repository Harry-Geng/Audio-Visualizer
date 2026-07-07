"""
Retry songs that never made it into the library — download failures from a prior
batch run. Reads batch_log.jsonl, finds every attempted title with no <id>_stems/
folder, and re-attempts each with a more resilient search than the batch used:
several query phrasings, more candidates, and candidate-fallback (so one dead or
newly-unavailable YouTube upload no longer kills the track).

  python retry_missing.py                 # everything still missing
  python retry_missing.py --filter coco   # only titles containing "coco"
  python retry_missing.py --dry-run       # list what would be retried
  python retry_missing.py --limit 5

Set AV_COOKIES_BROWSER=brave (or chrome/edge/firefox) to use a logged-in
browser's cookies for age-gated / bot-checked uploads.
"""

import os
import re
import sys
import json
import shutil
import tempfile
import argparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import LIBRARY_DIR
import ingest
import batch_spotify as bs


def _missing_titles():
    """[(title, spotify_id, duration_s)] for attempted titles with no stems dir.
    Deduped by title, keeping the last-seen metadata."""
    p = os.path.join(LIBRARY_DIR, "batch_log.jsonl")
    seen = {}
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("title"):
                seen[r["title"]] = r
    out = []
    for title, r in seen.items():
        slug = ingest._base_slug(title)
        if os.path.isdir(os.path.join(LIBRARY_DIR, slug + "_stems")):
            continue                                     # already recovered
        dur = r.get("duration_s") or r.get("yt_duration") or 0.0
        out.append((title, r.get("spotify_id"), dur))
    out.sort()
    return out


def _query_variants(title):
    """Looser phrasings tried in order. The bare title tends to surface the same
    dead 'official' upload the batch already failed on; the 'audio'/'lyrics'
    variants bias YouTube toward alternate re-uploads that are still live."""
    stripped = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()   # drop "(feat. …)" tails
    variants = [title, title + " audio"]
    if stripped and stripped != title:
        variants += [stripped + " audio", stripped]
    variants += [title + " official audio", title + " lyrics"]
    seen, uniq = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def main():
    ap = argparse.ArgumentParser(description="Retry missing downloads with a resilient search.")
    ap.add_argument("--filter", default=None,
                    help="only titles containing this substring (case-insensitive)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    missing = _missing_titles()
    if args.filter:
        f = args.filter.lower()
        missing = [m for m in missing if f in m[0].lower()]
    if args.limit:
        missing = missing[: args.limit]

    print(f"[retry] library dir: {LIBRARY_DIR}")
    print(f"[retry] {len(missing)} title(s) to retry"
          + (f"  (filter: {args.filter!r})" if args.filter else ""))
    if args.dry_run:
        for title, _sid, dur in missing:
            print(f"    - {title}  ({bs._fmt_dur(dur)})")
        return

    ok = fail = 0
    for i, (title, spotify_id, dur) in enumerate(missing, 1):
        print(f"\n[{i}/{len(missing)}]  {title}")
        tmpdir = tempfile.mkdtemp(prefix="retry_")
        flac = meta = None
        errs = []
        try:
            for q in _query_variants(title):
                try:
                    flac, meta = bs.download_track(q, dur, tmpdir, search_n=12, max_tries=8)
                    print(f"    found via {q!r} -> {meta.get('yt_title')}")
                    break
                except Exception as e:
                    errs.append(f"{q!r}: {str(e).splitlines()[-1][:80]}")

            if not flac:
                fail += 1
                print("    STILL MISSING — every search variant failed:")
                for e in errs:
                    print("        " + e)
                bs.log_result({"title": title, "spotify_id": spotify_id,
                               "status": "error", "error": "retry: " + " | ".join(errs[:4])})
                continue

            job = ingest.process_file_sync(flac, title, hq_vocals=True,
                                           drum_kit=True, lossy=True)
            rec = {"title": title, "spotify_id": spotify_id, "song_id": job.song_id, **meta}
            if job.error:
                fail += 1
                rec["status"] = "error"
                rec["error"] = job.error
                print(f"    FAILED (pipeline): {job.error}")
            else:
                ok += 1
                rec["status"] = "ok"
                print(f"    ok -> {job.song_id}")
            bs.log_result(rec)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n[retry] done — recovered {ok}, still missing {fail}")
    if ok:
        print("[retry] to fold the new songs into the map / text-search / lyrics, run:")
        print("        python compute_galaxy.py && python backfill_clap.py && python backfill_lyrics.py")


if __name__ == "__main__":
    main()
