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

from config import SR, HOP_LENGTH, FPS, DEMUCS_MODEL, STEM_NAMES
from feature_extractor import extract_all
from feature_writer import write_features

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# job registry
# ---------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()      # serialize heavy work
_MODEL = None
_MODEL_LOCK = threading.Lock()

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
def _slugify_id(title):
    """Filesystem-safe, unique song id derived from a title."""
    base = re.sub(r"[^\w\-. ]+", "", title).strip() or "track"
    base = base[:80]
    cand, n = base, 2
    while os.path.exists(os.path.join(HERE, f"{cand}_stems")):
        cand = f"{base} ({n})"
        n += 1
    return cand


def _load_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            _MODEL = get_model(DEMUCS_MODEL)
            _MODEL.eval()
        return _MODEL


def _separate_hq(path):
    """Return ({stem: stereo float32 [n,2]}, sr) at the model's native rate."""
    model = _load_model()
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
        sources = apply_model(model, wav_n.unsqueeze(0), progress=True)[0]
    sources = sources * std + mean       # back to original level

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
        else:
            sep.output_dir = outdir
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
        "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    title = info.get("title", "track")
    if info.get("uploader"):
        title = f"{info['uploader']} - {title}"
    flac = os.path.join(tmpdir, f"{info['id']}.flac")
    if not os.path.exists(flac):                      # fall back to whatever landed
        cands = [f for f in os.listdir(tmpdir)]
        flac = os.path.join(tmpdir, cands[0])
    return flac, title


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def _run(job, src_path=None, url=None, hq_vocals=False, drum_kit=False):
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
            orig_dest = os.path.join(HERE, f"{song_id}.flac")
            y_orig, sr_orig = sf.read(src_path, always_2d=True)
            sf.write(orig_dest, y_orig, sr_orig)

            use_hq_voc = hq_vocals and separator_available()
            use_drumsep = drum_kit and separator_available()

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

            # 2b. htdemucs for drums/bass/other (+ vocals we may discard)
            sep_base = job.progress or 0
            sep_top = 70 if use_drumsep else 86
            _set(job, "separating", "separating stems with Demucs (a few minutes)…")
            _prog.update(job=job, base=sep_base, span=sep_top - sep_base)
            try:
                stems_hq, sr_hq = _separate_hq(demucs_src)
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
                    sf.write(drums_path, stems_hq["drums"], sr_hq)
                    parts, p_sr = _separate_drums(drums_path, tmpdir)
                    for name, arr in parts.items():
                        if p_sr != sr_hq:
                            arr = librosa.resample(arr.T, orig_sr=p_sr, target_sr=sr_hq).T
                        drum_parts[name] = arr.astype(np.float32)
                except Exception as e:
                    print(f"  DrumSep failed, keeping band-split kit: {e}")
                    use_drumsep = False
                job.progress = 86

            hq_dir = os.path.join(HERE, f"{song_id}_stems_hq")
            an_dir = os.path.join(HERE, f"{song_id}_stems")
            os.makedirs(hq_dir, exist_ok=True)
            os.makedirs(an_dir, exist_ok=True)

            def _save(name, arr_stereo):
                sf.write(os.path.join(hq_dir, f"{name}.wav"), arr_stereo, sr_hq, subtype="PCM_16")
                mono = _to_analysis(arr_stereo, sr_hq)
                sf.write(os.path.join(an_dir, f"{name}.wav"), mono, SR, subtype="PCM_16")
                return mono

            analysis = {}
            for name in STEM_NAMES:                 # base stems feed feature extraction
                analysis[name] = _save(name, stems_hq[name])
            for name, arr in drum_parts.items():     # DrumSep kit parts (waveforms only)
                _save(name, arr)

            # 3. features
            job.progress = 90
            _set(job, "features", "extracting features…")
            n = min(len(a) for a in analysis.values())
            analysis = {k: v[:n] for k, v in analysis.items()}
            mix = sum(analysis.values()).astype(np.float32)
            feats = extract_all(analysis, mix)
            feats["meta"] = {
                "filename": f"{song_id}.flac",
                "duration_seconds": round(n / SR, 6),
                "sample_rate": SR, "hop_length": HOP_LENGTH,
                "fps": round(FPS, 6),
                "n_frames": len(feats["macro"]["energy_envelope"]),
                "processed_at": "", "lossy_source": job.lossy,
                "hq_vocals": use_hq_voc, "drum_kit": use_drumsep,
            }
            write_features(feats, orig_dest)

            if on_song_ready:
                on_song_ready(song_id, an_dir)
            job.progress = 100
            _set(job, "done", f"ready: {song_id}")
    except Exception as e:
        job.error = str(e)
        _set(job, "error", f"failed: {e}")
        traceback.print_exc()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_file_job(src_path, title, hq_vocals=False, drum_kit=False):
    job = Job("file")
    job.title = title
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run, daemon=True, kwargs={
        "job": job, "src_path": src_path, "hq_vocals": hq_vocals, "drum_kit": drum_kit,
    }).start()
    return job.as_dict()


def start_url_job(url, hq_vocals=False, drum_kit=False):
    job = Job("url")
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run, daemon=True, kwargs={
        "job": job, "url": url, "hq_vocals": hq_vocals, "drum_kit": drum_kit,
    }).start()
    return job.as_dict()
