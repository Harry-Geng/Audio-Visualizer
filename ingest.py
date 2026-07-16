"""
Background ingestion pipeline for the Music Microscope.

Two entry points feed one pipeline:
  - a dropped/uploaded audio file
  - a pasted URL (downloaded with yt-dlp)

The pipeline runs Demucs once and keeps BOTH:
  - full-rate stereo stems  -> <id>_stems_hq/   (for high-quality solo/mute)
  - 22 kHz mono stems       -> <id>_stems/      (analysis source the scope reads)
plus a features JSON (beats / sections / pitch ...) so overlays light up.

Jobs run on a single worker thread (Demucs is heavy) and expose coarse stage
progress that the browser polls.
"""

import os
import re
import uuid
import shutil
import threading
import tempfile
import traceback

import numpy as np
import torch
import torchaudio
import librosa
import soundfile as sf
import tqdm as _tqdm_mod
from demucs.pretrained import get_model
from demucs.apply import apply_model

# Demucs (4.0.1) has no progress callback, but with progress=True it wraps the
# chunk loop in tqdm.tqdm. We subclass tqdm to report real fractional progress
# into the active job, then monkeypatch it in so apply_model uses ours.
_prog = {"job": None, "base": 0, "span": 0}


class _ReportingTqdm(_tqdm_mod.tqdm):
    def update(self, n=1):
        r = super().update(n)
        job = _prog["job"]
        if job is not None and self.total:
            job.progress = int(_prog["base"] + self.n / self.total * _prog["span"])
        return r


_tqdm_mod.tqdm = _ReportingTqdm
import demucs.apply as _demucs_apply          # noqa: E402
_demucs_apply.tqdm = _tqdm_mod

from config import SR, HOP_LENGTH, FPS, DEMUCS_MODEL, STEM_NAMES, LIBRARY_DIR
from feature_extractor import extract_all
from feature_writer import write_features
from moment_index import build_song_moments

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# job registry
# ---------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()      # serialize heavy work
_MODELS = {}                         # name -> loaded demucs model
_MODEL_LOCK = threading.Lock()
DEMUCS_6S_MODEL = "htdemucs_6s"      # adds guitar + piano (piano is weak)


def _pick_device():
    """cuda -> mps -> cpu. Demucs defaults to the *input tensor's* device, so we
    must move both model and audio here or htdemucs silently runs on CPU."""
    if torch.cuda.is_available():
        return torch.device("cuda")
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


DEVICE = _pick_device()
print(f"[ingest] torch device: {DEVICE}"
      + (f" ({torch.cuda.get_device_name(0)})" if DEVICE.type == "cuda" else ""))

# Ampere+ free speedups for the conv-heavy separation models: TF32 matmul/conv
# (negligible precision change for audio stems) and cudnn autotuning of kernels
# (the per-song input shape is stable, so the autotune cost is paid once).
if DEVICE.type == "cuda":
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True

# called by microscope.py after a song finishes so the server can serve it
on_song_ready = None                 # fn(song_id, stems_dir)


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind            # "file" | "url"
        self.stage = "queued"       # queued|downloading|separating|features|done|error
        self.message = "waiting…"
        self.progress = 0           # 0..100
        self.song_id = None
        self.title = None
        self.lossy = False
        self.error = None

    def as_dict(self):
        return {
            "id": self.id, "kind": self.kind, "stage": self.stage,
            "message": self.message, "progress": self.progress, "song_id": self.song_id,
            "title": self.title, "lossy": self.lossy, "error": self.error,
        }


def get_job(job_id):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        return j.as_dict() if j else None


def _set(job, stage, message):
    job.stage = stage
    job.message = message


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _base_slug(title):
    """Deterministic filesystem-safe id from a title (no uniqueness suffix).

    The batch runner uses this to predict a song's id and skip it if already
    processed, so it must stay in sync with _slugify_id's base.
    """
    base = re.sub(r"[^\w\-. ]+", "", title).strip() or "track"
    return base[:80]


def _slugify_id(title):
    """Filesystem-safe, unique song id derived from a title."""
    base = _base_slug(title)
    cand, n = base, 2
    while os.path.exists(os.path.join(LIBRARY_DIR, f"{cand}_stems")):
        cand = f"{base} ({n})"
        n += 1
    return cand


def _load_model(name=DEMUCS_MODEL):
    with _MODEL_LOCK:
        m = _MODELS.get(name)
        if m is None:
            m = get_model(name)
            m.to(DEVICE)
            m.eval()
            _MODELS[name] = m
        return m


def _separate_hq(path, model_name=DEMUCS_MODEL):
    """Return ({stem: stereo float32 [n,2]}, sr) at the model's native rate."""
    model = _load_model(model_name)
    wav, sr = torchaudio.load(path)
    if sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)
    sr = model.samplerate
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    elif wav.shape[0] > 2:
        wav = wav[:2]

    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std()
    wav_n = (wav - mean) / (std + 1e-8)
    with torch.no_grad():
        sources = apply_model(model, wav_n.unsqueeze(0).to(DEVICE),
                              device=DEVICE, progress=True)[0]
    sources = sources.cpu() * std + mean       # back to original level (std/mean are CPU)

    stems = {}
    for i, name in enumerate(model.sources):
        stems[name] = sources[i].transpose(0, 1).contiguous().numpy().astype(np.float32)  # [n, 2]
    return stems, sr


# audio-separator specialist models, loaded lazily and cached one Separator per
# model (so neither gets reloaded between jobs).
ROFORMER_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"   # SOTA vocal isolation
DRUMSEP_MODEL = "MDX23C-DrumSep-aufr33-jarredou.ckpt"          # kick/snare/toms/hh/ride/crash
DRUM_PARTS = ["kick", "snare", "toms", "hh", "ride", "crash"]
_SEPARATORS = {}
_SEP_LOCK = threading.Lock()


def separator_available():
    try:
        import audio_separator.separator  # noqa: F401
        return True
    except Exception:
        return False


# kept for callers/back-compat
def roformer_available():
    return separator_available()


def _get_separator(model_filename, outdir):
    import logging as _lg
    from audio_separator.separator import Separator
    with _SEP_LOCK:
        sep = _SEPARATORS.get(model_filename)
        if sep is None:
            sep = Separator(output_dir=outdir, output_format="WAV", log_level=_lg.WARNING)
            sep.load_model(model_filename=model_filename)
            _SEPARATORS[model_filename] = sep
        # Point BOTH the Separator and its loaded model_instance at this job's
        # outdir. The model_instance does the actual writing and caches its own
        # output_dir from load_model time, so reassigning only sep.output_dir
        # (as before) made every reused separator write into the previous job's
        # already-deleted temp dir -> "System error" on read. Sync both.
        sep.output_dir = outdir
        mi = getattr(sep, "model_instance", None)
        if mi is not None:
            mi.output_dir = outdir
        return sep


def _read_stereo(outdir, fname):
    y, sr = sf.read(os.path.join(outdir, fname), always_2d=True)
    return y.astype(np.float32), sr


def _separate_roformer(path, outdir):
    """BS-Roformer -> (vocals [n,2], instrumental_path, sr)."""
    sep = _get_separator(ROFORMER_MODEL, outdir)
    files = sep.separate(path)
    voc_f = next((f for f in files if "vocal" in f.lower()), None)
    inst_f = next((f for f in files if "instrumental" in f.lower()), None)
    if voc_f is None or inst_f is None:
        raise RuntimeError("Roformer produced unexpected outputs")
    voc, sr = _read_stereo(outdir, voc_f)
    return voc, os.path.join(outdir, inst_f), sr


def _separate_drums(drums_path, outdir):
    """DrumSep -> {part: [n,2]}, sr  for kick/snare/toms/hh/ride/crash."""
    sep = _get_separator(DRUMSEP_MODEL, outdir)
    files = sep.separate(drums_path)
    parts = {}
    for p in DRUM_PARTS:
        f = next((x for x in files if f"({p})" in x.lower() or f"_{p}_" in x.lower()), None)
        if f:
            arr, sr = _read_stereo(outdir, f)
            parts[p] = arr
    if not parts:
        raise RuntimeError("DrumSep produced no parts")
    return parts, sr


def _to_analysis(stem_stereo, sr):
    """Stereo full-rate -> mono 22 kHz for feature extraction / visuals."""
    mono = stem_stereo.mean(axis=1)
    if sr != SR:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=SR)
    return mono.astype(np.float32)


def _download_url(url, job, tmpdir):
    """Download best audio with yt-dlp, extract losslessly to FLAC."""
    import yt_dlp
    info_box = {}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job.progress = int(min(1.0, d.get("downloaded_bytes", 0) / total) * 8)
            pct = d.get("_percent_str", "").strip()
            _set(job, "downloading", f"downloading… {pct}")
        elif d["status"] == "finished":
            job.progress = 8
            _set(job, "downloading", "download complete, converting…")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}],
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):                           # playlist URL → first entry's info
        info = next((e for e in info["entries"] if e), info)
    title = info.get("title", "track")
    if info.get("uploader"):
        title = f"{info['uploader']} - {title}"
    flac = os.path.join(tmpdir, f"{info.get('id')}.flac")
    if not os.path.exists(flac):                      # fall back to a produced audio file
        cands = [f for f in os.listdir(tmpdir) if f.endswith(".flac")] \
            or [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl"))]
        if not cands:
            raise RuntimeError("download produced no audio file")
        flac = os.path.join(tmpdir, cands[0])
    return flac, title


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def _run(job, src_path=None, url=None, hq_vocals=False, drum_kit=False, six_stem=False):
    tmpdir = tempfile.mkdtemp(prefix="microscope_")
    try:
        with _WORKER_LOCK:
            if url:
                job.lossy = True
                src_path, title = _download_url(url, job, tmpdir)
            else:
                title = job.title or os.path.splitext(os.path.basename(src_path))[0]

            song_id = _slugify_id(title)
            job.song_id, job.title = song_id, title

            # 1. keep a soundfile-readable original for full-quality playback
            _set(job, "separating", "saving original…")
            orig_dest = os.path.join(LIBRARY_DIR, f"{song_id}.flac")
            try:
                y_orig, sr_orig = sf.read(src_path, always_2d=True)
            except (RuntimeError, sf.LibsndfileError):
                # libsndfile can't decode m4a/aac although the UI accepts them;
                # fall back to librosa (audioread/ffmpeg) and reshape its
                # (ch, n) output to the (n, ch) shape sf.read returns
                y_lr, sr_orig = librosa.load(src_path, sr=None, mono=False)
                y_orig = np.atleast_2d(y_lr).T
            sf.write(orig_dest, y_orig, sr_orig)

            use_hq_voc = hq_vocals and separator_available()
            use_drumsep = drum_kit and separator_available()
            demucs_model = DEMUCS_6S_MODEL if six_stem else DEMUCS_MODEL

            # 2. Vocal isolation + cascade: when HQ vocals are on, run BS-Roformer
            #    first (vocals + instrumental), then run htdemucs on the *instrumental*
            #    so drums/bass/other come out with less vocal bleed (measured cleaner).
            voc_override = None
            if use_hq_voc:
                _set(job, "separating", "isolating HQ vocals (BS-Roformer)…")
                job.progress = 10
                try:
                    voc, inst_path, voc_sr = _separate_roformer(src_path, tmpdir)
                    voc_override = (voc, voc_sr)
                    demucs_src = inst_path                     # cascade input
                    job.progress = 40
                except Exception as e:
                    print(f"  HQ vocals/cascade failed, falling back to mix: {e}")
                    use_hq_voc, demucs_src = False, src_path
            else:
                demucs_src = src_path

            # 2b. htdemucs for drums/bass/other (+ vocals we may discard). With
            #     6-stem, this also yields guitar + piano.
            sep_base = job.progress or 0
            sep_top = 70 if use_drumsep else 86
            msg = "separating 6 stems (Demucs 6s)…" if six_stem \
                else "separating stems with Demucs (a few minutes)…"
            _set(job, "separating", msg)
            _prog.update(job=job, base=sep_base, span=sep_top - sep_base)
            try:
                stems_hq, sr_hq = _separate_hq(demucs_src, demucs_model)
            finally:
                _prog.update(job=None, base=0, span=0)
            job.progress = sep_top

            if voc_override is not None:
                voc, voc_sr = voc_override
                if voc_sr != sr_hq:
                    voc = librosa.resample(voc.T, orig_sr=voc_sr, target_sr=sr_hq).T
                stems_hq["vocals"] = voc.astype(np.float32)

            # 2c. DrumSep: split the drums stem into real kit parts
            drum_parts = {}
            if use_drumsep:
                _set(job, "separating", "splitting drum kit (DrumSep)…")
                try:
                    drums_path = os.path.join(tmpdir, "_drums.wav")
                    # float WAV: PCM_16 would clip >±1 peaks before DrumSep sees them
                    sf.write(drums_path, stems_hq["drums"], sr_hq, subtype="FLOAT")
                    parts, p_sr = _separate_drums(drums_path, tmpdir)
                    for name, arr in parts.items():
                        if p_sr != sr_hq:
                            arr = librosa.resample(arr.T, orig_sr=p_sr, target_sr=sr_hq).T
                        drum_parts[name] = arr.astype(np.float32)
                except Exception as e:
                    print(f"  DrumSep failed, keeping band-split kit: {e}")
                    use_drumsep = False
                job.progress = 86

            # Separation outputs routinely overshoot ±1.0; FLAC PCM_16 would
            # hard-clip them on write. Rescale ALL stems (and kit parts) by one
            # shared factor so relative levels — and mix reconstruction — hold.
            peak = max([np.abs(a).max() for a in stems_hq.values()]
                       + [np.abs(a).max() for a in drum_parts.values()] + [1.0])
            if peak > 1.0:
                g = 0.999 / peak
                stems_hq = {k: v * g for k, v in stems_hq.items()}
                drum_parts = {k: v * g for k, v in drum_parts.items()}

            # trim everything to one common length BEFORE writing, so the files
            # on disk match the features/meta computed from them
            n_hq = min(len(a) for a in list(stems_hq.values()) + list(drum_parts.values()))
            stems_hq = {k: v[:n_hq] for k, v in stems_hq.items()}
            drum_parts = {k: v[:n_hq] for k, v in drum_parts.items()}

            hq_dir = os.path.join(LIBRARY_DIR, f"{song_id}_stems_hq")
            an_dir = os.path.join(LIBRARY_DIR, f"{song_id}_stems")
            os.makedirs(hq_dir, exist_ok=True)
            os.makedirs(an_dir, exist_ok=True)

            def _save(name, arr_stereo):
                # FLAC = lossless + ~4x smaller than PCM_16 WAV (verified). soundfile
                # picks FLAC from the extension; default subtype is PCM_16 (parity).
                sf.write(os.path.join(hq_dir, f"{name}.flac"), arr_stereo, sr_hq, subtype="PCM_16")
                mono = _to_analysis(arr_stereo, sr_hq)
                sf.write(os.path.join(an_dir, f"{name}.flac"), mono, SR, subtype="PCM_16")
                return mono

            # base stems = the 4 standard + any extras from 6-stem (guitar/piano)
            base_names = list(STEM_NAMES) + [n for n in stems_hq if n not in STEM_NAMES]
            analysis = {}
            for name in base_names:
                analysis[name] = _save(name, stems_hq[name])
            for name, arr in drum_parts.items():     # DrumSep kit parts (waveforms only)
                _save(name, arr)

            # 3. features (per-stem on the standard 4; mix = sum of all base stems)
            job.progress = 90
            _set(job, "features", "extracting features…")
            n = min(len(a) for a in analysis.values())
            analysis = {k: v[:n] for k, v in analysis.items()}
            mix = sum(analysis[k] for k in base_names).astype(np.float32)
            feats = extract_all(analysis, mix)
            feats["meta"] = {
                "filename": f"{song_id}.flac",
                "duration_seconds": round(n / SR, 6),
                "sample_rate": SR, "hop_length": HOP_LENGTH,
                "fps": round(FPS, 6),
                "n_frames": len(feats["macro"]["energy_envelope"]),
                "processed_at": "", "lossy_source": job.lossy,
                "hq_vocals": use_hq_voc, "drum_kit": use_drumsep, "six_stem": six_stem,
            }
            write_features(feats, orig_dest)

            # 4. moment index: per-moment interaction/descriptor/MERT facets for
            #    the similarity + taste-mapping engine. Non-fatal: a failure here
            #    must not lose the separated stems / features.
            job.progress = 96
            _set(job, "moments", "building moment index (MERT embeddings)…")
            try:
                build_song_moments(song_id, an_dir, feats, verbose=False)
            except Exception as e:
                print(f"  moment index failed (stems/features kept): {e}")

            # 5. lyrics: LRCLIB official text + word-level forced alignment to the
            #    vocal stem (karaoke). Non-fatal. Skipped when AV_SKIP_LYRICS is set
            #    (e.g. the bulk batch, so the 1.2 GB aligner doesn't compete with the
            #    separation models for VRAM) — run backfill_lyrics.py afterwards.
            job.progress = 98
            if not os.environ.get("AV_SKIP_LYRICS"):
                _set(job, "lyrics", "fetching + aligning lyrics…")
                try:
                    import lyrics as _lyrics
                    artist, track = (title.split(" - ", 1) + [""])[:2] if " - " in title else ("", title)
                    _lyrics.build_song_lyrics(song_id, artist, track,
                                              feats["meta"]["duration_seconds"], an_dir, verbose=False)
                except Exception as e:
                    print(f"  lyrics failed (song kept): {e}")

            if on_song_ready:
                on_song_ready(song_id, an_dir)
            job.progress = 100
            _set(job, "done", f"ready: {song_id}")
    except Exception as e:
        job.error = str(e)
        _set(job, "error", f"failed: {e}")
        traceback.print_exc()
        # remove half-written library artifacts unless the song reached the
        # features stage — a stems dir without features looks "done" to skip
        # checks yet renders broken, and blocks re-ingest under the same id
        sid = getattr(job, "song_id", None)
        if sid and not os.path.exists(os.path.join(LIBRARY_DIR, f"{sid}_features.json")):
            for p in (f"{sid}_stems", f"{sid}_stems_hq"):
                shutil.rmtree(os.path.join(LIBRARY_DIR, p), ignore_errors=True)
            try:
                os.remove(os.path.join(LIBRARY_DIR, f"{sid}.flac"))
            except OSError:
                pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_file_sync(src_path, title, hq_vocals=False, drum_kit=False,
                      six_stem=False, lossy=False):
    """Run the full pipeline synchronously (no worker thread) and return the Job.

    Used by the batch runner, which processes songs one at a time and inspects
    job.error / job.song_id after each. The browser path uses the threaded
    start_*_job functions instead.
    """
    job = Job("file")
    job.title = title
    job.lossy = lossy
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    _run(job, src_path=src_path, hq_vocals=hq_vocals,
         drum_kit=drum_kit, six_stem=six_stem)
    return job


def start_file_job(src_path, title, hq_vocals=False, drum_kit=False, six_stem=False):
    job = Job("file")
    job.title = title
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run, daemon=True, kwargs={
        "job": job, "src_path": src_path, "hq_vocals": hq_vocals,
        "drum_kit": drum_kit, "six_stem": six_stem,
    }).start()
    return job.as_dict()


def start_url_job(url, hq_vocals=False, drum_kit=False, six_stem=False):
    job = Job("url")
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run, daemon=True, kwargs={
        "job": job, "url": url, "hq_vocals": hq_vocals,
        "drum_kit": drum_kit, "six_stem": six_stem,
    }).start()
    return job.as_dict()
