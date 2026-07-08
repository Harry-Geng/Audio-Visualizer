"""
Build a self-contained demo_library/ from a folder of audio files — the data the
hosted preview serves (see docs/deploy-demo.md). Run this on your GPU machine.

Use ONLY music you're allowed to redistribute (Creative Commons / royalty-free),
because the demo streams the audio publicly.

  python build_demo_library.py --src ./demo_songs
  python build_demo_library.py --src ./demo_songs --out ./demo_library

Each file is analyzed at the demo tier (HQ vocals + drum kit); then CLAP
embeddings and the galaxy layout are computed. The result is ready to deploy.
"""

import os
import sys
import glob
import argparse
import subprocess


def main():
    ap = argparse.ArgumentParser(description="Build a demo library from CC audio files.")
    ap.add_argument("--src", required=True, help="folder of audio files (CC / royalty-free)")
    ap.add_argument("--out", default="demo_library", help="output library dir")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    # config reads AV_LIBRARY_DIR at import → set it before importing ingest
    os.environ["AV_LIBRARY_DIR"] = out
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import ingest  # noqa: E402  (must follow the AV_LIBRARY_DIR assignment)

    exts = (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".aiff", ".aif")
    files = sorted(f for f in glob.glob(os.path.join(args.src, "*"))
                   if f.lower().endswith(exts))
    if not files:
        sys.exit(f"no audio files found in {args.src}")
    print(f"[demo] {len(files)} song(s) -> {out}")

    ok = 0
    for i, f in enumerate(files, 1):
        title = os.path.splitext(os.path.basename(f))[0]
        print(f"[{i}/{len(files)}] {title}")
        job = ingest.process_file_sync(f, title, hq_vocals=True, drum_kit=True, lossy=False)
        if job.error:
            print(f"    FAILED: {job.error}")
        else:
            ok += 1
            print(f"    ok -> {job.song_id}")

    if not ok:
        sys.exit("[demo] nothing processed — aborting before index build")

    env = dict(os.environ, AV_LIBRARY_DIR=out)
    py = sys.executable
    print("[demo] CLAP embeddings ...")
    subprocess.run([py, "backfill_clap.py"], env=env, check=False)
    print("[demo] galaxy layout ...")
    subprocess.run([py, "compute_galaxy.py"], env=env, check=False)

    print(f"\n[demo] done — {ok} song(s). Library ready at {out}")
    print("[demo] next: follow docs/deploy-demo.md to publish the hosted preview.")


if __name__ == "__main__":
    main()
