"""
Backfill CLAP audio embeddings for every moment in the library — the audio half
of text -> moment search ("dark moody bassline" -> matching moments across all
songs). Uses laion/larger_clap_music, whose text and audio encoders share one
512-dim space, so a text query embeds directly against these vectors.

One <id>_clap.npz per song:
    emb       (n_moments, 512) float32, L2-normalised CLAP audio embeddings
    start_t   copied from <id>_moments.npz (alignment safety)
    end_t

Resumable: a song is skipped when its _clap.npz already has one row per moment.

  python backfill_clap.py              # everything missing
  python backfill_clap.py --limit 2    # spot check
  python backfill_clap.py --force      # rebuild
  python backfill_clap.py --batch 8    # smaller batches (gentler on VRAM)
"""

import os
import sys
import glob
import time
import argparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
import soundfile as sf
import torch

from config import LIBRARY_DIR, stem_file

# NB: laion/larger_clap_music is a broken HF conversion (collapsed embeddings,
# verified against noise/tone/music probes) — the reference checkpoint works.
MODEL_ID = "laion/clap-htsat-unfused"
CLAP_SR = 48000
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

_model = None
_processor = None


def _load_model():
    global _model, _processor
    if _model is None:
        from transformers import ClapModel, ClapProcessor
        print(f"[clap] loading {MODEL_ID} on {DEVICE} ...", flush=True)
        _processor = ClapProcessor.from_pretrained(MODEL_ID)
        _model = ClapModel.from_pretrained(MODEL_ID).to(DEVICE).eval()
    return _model, _processor


def _load_audio_48k(song_id, stems_dir):
    """Full-song mono 48 kHz float32. Prefers the original file (full band);
    falls back to the 22 kHz analysis mix if the original can't be decoded."""
    candidates = []
    for ext in (".flac", ".wav", ".mp3", ".ogg", ".m4a", ".aac"):
        p = os.path.join(LIBRARY_DIR, song_id + ext)
        if os.path.exists(p):
            candidates.append(p)
            break
    mix = stem_file(stems_dir, "mix")
    if mix:
        candidates.append(mix)
    for src in candidates:
        try:
            y, sr = sf.read(src, dtype="float32", always_2d=True)
        except Exception:
            continue
        y = y.mean(axis=1)
        if sr != CLAP_SR:
            import librosa
            y = librosa.resample(y, orig_sr=sr, target_sr=CLAP_SR)
        return np.ascontiguousarray(y, dtype=np.float32)
    raise FileNotFoundError(f"no decodable audio for {song_id}")


@torch.no_grad()
def embed_song(song_id, stems_dir, batch=8):
    d = np.load(os.path.join(LIBRARY_DIR, f"{song_id}_moments.npz"))
    starts, ends = d["start_t"], d["end_t"]
    y = _load_audio_48k(song_id, stems_dir)
    model, proc = _load_model()
    embs = []
    for i in range(0, len(starts), batch):
        clips = []
        for s, e in zip(starts[i:i + batch], ends[i:i + batch]):
            a, b = int(s * CLAP_SR), min(int(e * CLAP_SR), len(y))
            seg = y[a:b]
            if seg.size < CLAP_SR // 4:            # degenerate tail moment
                seg = np.zeros(CLAP_SR // 4, np.float32)
            clips.append(seg)
        try:    # transformers 5.x renamed `audios` -> `audio`
            inputs = proc(audio=clips, sampling_rate=CLAP_SR,
                          return_tensors="pt", padding=True)
        except (TypeError, ValueError):
            inputs = proc(audios=clips, sampling_rate=CLAP_SR,
                          return_tensors="pt", padding=True)
        inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
        out = model.get_audio_features(**inputs)
        emb = out.pooler_output if hasattr(out, "pooler_output") else out
        emb = torch.nn.functional.normalize(emb, dim=-1)   # idempotent
        embs.append(emb.cpu().float().numpy())
    emb = np.concatenate(embs, axis=0).astype(np.float32)
    np.savez_compressed(os.path.join(LIBRARY_DIR, f"{song_id}_clap.npz"),
                        emb=emb, start_t=starts, end_t=ends)
    return emb.shape[0]


def _todo(force=False):
    """(song_id, stems_dir, n_moments) for songs missing/outdated _clap.npz."""
    out = []
    for f in sorted(glob.glob(os.path.join(LIBRARY_DIR, "*_moments.npz"))):
        sid = os.path.basename(f)[: -len("_moments.npz")]
        sdir = os.path.join(LIBRARY_DIR, sid + "_stems")
        try:
            n = np.load(f)["start_t"].shape[0]
        except Exception as ex:      # corrupt moments npz → skip, don't kill the scan
            print(f"[clap] skipping {sid}: unreadable moments file ({ex})")
            continue
        outp = os.path.join(LIBRARY_DIR, f"{sid}_clap.npz")
        if not force and os.path.exists(outp):
            try:
                if np.load(outp)["emb"].shape[0] == n:
                    continue
            except Exception:
                pass                                # corrupt -> redo
        out.append((sid, sdir, n))
    return out


def main():
    ap = argparse.ArgumentParser(description="Backfill CLAP moment embeddings.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    todo = _todo(args.force)
    if args.limit:
        todo = todo[: args.limit]
    total = len(todo)
    print(f"[clap] library dir: {LIBRARY_DIR}")
    print(f"[clap] {total} songs to embed", flush=True)

    done = fail = 0
    t_start = time.time()
    for sid, sdir, n in todo:
        t0 = time.time()
        try:
            rows = embed_song(sid, sdir, batch=args.batch)
            done += 1
            rate = (time.time() - t_start) / done
            eta_min = rate * (total - done - fail) / 60
            print(f"[clap] {done + fail}/{total}  {sid}  "
                  f"({rows} moments, {time.time() - t0:.1f}s, eta {eta_min:.0f}m)",
                  flush=True)
        except Exception as e:
            fail += 1
            print(f"[clap] {done + fail}/{total}  {sid}  FAILED: {e}", flush=True)

    print(f"\n[clap] done: {done}  failed: {fail}  "
          f"({(time.time() - t_start) / 60:.1f} min)")


if __name__ == "__main__":
    main()
