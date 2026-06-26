"""
Layer C — learned per-stem embeddings via MERT.

MERT (m-a-p/MERT-v1-95M) is a music-specific self-supervised model. We run it
once per stem to get frame-level embeddings (~75 fps, 768-d, averaged across
its 13 hidden layers), then mean-pool the frames inside each moment window.
So a moment's embedding for, say, the bass stem captures what that bass *sounds
like* during those ~5 seconds — enabling "find similar bass moments".

The model is loaded lazily and cached; it runs on CUDA when available.
"""

import os
import numpy as np
import torch
import librosa

from config import SR

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

MERT_NAME = "m-a-p/MERT-v1-95M"
MERT_SR = 24000
EMB_DIM = 768

_model = None
_fe = None
_device = None


def _load():
    global _model, _fe, _device
    if _model is not None:
        return
    from transformers import AutoModel, Wav2Vec2FeatureExtractor
    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _fe = Wav2Vec2FeatureExtractor.from_pretrained(MERT_NAME, trust_remote_code=True)
    _model = AutoModel.from_pretrained(MERT_NAME, trust_remote_code=True)
    _model.to(_device).eval()


def frame_embeddings(wave, sr=SR, chunk_s=24):
    """Return (frame_emb [T, 768] float32, fps) for a mono waveform.

    The waveform is resampled to MERT's 24 kHz and run in chunks to bound GPU
    memory; per-layer hidden states are averaged into one 768-d vector/frame.
    """
    _load()
    if sr != MERT_SR:
        wave = librosa.resample(np.asarray(wave, dtype=np.float32),
                                orig_sr=sr, target_sr=MERT_SR)
    wave = np.ascontiguousarray(wave, dtype=np.float32)
    n = wave.size
    if n < MERT_SR // 2:                       # pad very short stems to ~0.5 s
        wave = np.pad(wave, (0, MERT_SR // 2 - n))
        n = wave.size

    chunk = int(chunk_s * MERT_SR)
    pieces = []
    with torch.no_grad():
        for s in range(0, n, chunk):
            seg = wave[s:s + chunk]
            if seg.size < MERT_SR // 20:        # skip <50 ms slivers
                continue
            inp = _fe(seg, sampling_rate=MERT_SR, return_tensors="pt")
            inp = {k: v.to(_device) for k, v in inp.items()}
            out = _model(**inp, output_hidden_states=True)
            hs = torch.stack(out.hidden_states)        # [layers, 1, t, 768]
            emb = hs.mean(dim=0)[0]                     # avg layers -> [t, 768]
            pieces.append(emb.cpu().numpy().astype(np.float32))
    if not pieces:
        return np.zeros((1, EMB_DIM), np.float32), 1.0
    frame_emb = np.concatenate(pieces, axis=0)
    fps = frame_emb.shape[0] / (n / MERT_SR)
    return frame_emb, fps


def pool_moments(frame_emb, fps, moments):
    """Mean-pool frame embeddings into one vector per moment → [n_moments, 768]."""
    T = frame_emb.shape[0]
    out = np.zeros((len(moments), EMB_DIM), np.float32)
    for m in moments:
        f0 = max(0, int(m.start_t * fps))
        f1 = min(T, int(m.end_t * fps))
        if f1 <= f0:
            f1 = min(T, f0 + 1)
        out[m.idx] = frame_emb[f0:f1].mean(axis=0)
    return out


def stem_moment_embeddings(wave, moments, sr=SR):
    """Convenience: waveform + moments → [n_moments, 768] MERT embeddings."""
    frame_emb, fps = frame_embeddings(wave, sr=sr)
    return pool_moments(frame_emb, fps, moments)
