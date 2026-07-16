import numpy as np
import torch
import torchaudio
import librosa
from demucs.pretrained import get_model
from demucs.apply import apply_model

from config import SR, DEMUCS_MODEL


def separate_stems(audio_path: str) -> dict[str, np.ndarray]:
    model = get_model(DEMUCS_MODEL)
    model.eval()

    wav, src_sr = torchaudio.load(audio_path)

    if src_sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, src_sr, model.samplerate)

    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    elif wav.shape[0] > 2:            # 5.1 etc. — demucs expects 2 channels
        wav = wav[:2]

    # Demucs expects normalized input
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std()
    wav = (wav - mean) / (std + 1e-8)

    with torch.no_grad():
        sources = apply_model(model, wav.unsqueeze(0), progress=True)[0]
    sources = sources * (std + 1e-8) + mean   # undo the normalization → true levels

    stems = {}
    for i, name in enumerate(model.sources):
        mono = sources[i].mean(0).numpy()
        stems[name] = librosa.resample(mono, orig_sr=model.samplerate, target_sr=SR)

    return stems
