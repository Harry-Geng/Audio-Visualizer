import numpy as np
import librosa
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, sosfiltfilt

from config import SR, HOP_LENGTH, STEM_NAMES

# Krumhansl-Kessler key profiles
_KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                       2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                       2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# smoothing window in frames (~0.15s at 43 fps)
_SMOOTH = 7


def _estimate_key_mode(waveform: np.ndarray) -> tuple[int, int]:
    chroma = librosa.feature.chroma_cqt(y=waveform, sr=SR, hop_length=HOP_LENGTH)
    chroma_mean = chroma.mean(axis=1)
    chroma_mean /= chroma_mean.sum() + 1e-8

    major_scores = [
        np.corrcoef(np.roll(_KK_MAJOR, i), chroma_mean)[0, 1]
        for i in range(12)
    ]
    minor_scores = [
        np.corrcoef(np.roll(_KK_MINOR, i), chroma_mean)[0, 1]
        for i in range(12)
    ]

    best_major = int(np.argmax(major_scores))
    best_minor = int(np.argmax(minor_scores))

    if major_scores[best_major] >= minor_scores[best_minor]:
        return best_major, 1
    return best_minor, 0


def _dynamic_gradient(rms: np.ndarray) -> np.ndarray:
    """Rate of change of energy — positive = crescendo, negative = decrescendo."""
    grad = np.gradient(rms)
    return uniform_filter1d(grad, size=_SMOOTH * 3)


def _attack_envelope(onset_strength: np.ndarray, rms: np.ndarray) -> np.ndarray:
    """Ratio of onset sharpness to sustain — high = staccato, low = legato."""
    sustain = uniform_filter1d(rms, size=_SMOOTH * 2) + 1e-10
    attack = onset_strength / sustain
    return uniform_filter1d(attack, size=_SMOOTH)


def _spectral_flux(waveform: np.ndarray) -> np.ndarray:
    """Frame-to-frame change in spectral shape — high = timbral movement."""
    S = np.abs(librosa.stft(y=waveform, hop_length=HOP_LENGTH))
    # normalise each frame so we measure shape change, not volume change
    S_norm = S / (S.sum(axis=0, keepdims=True) + 1e-10)
    flux = np.sqrt(np.sum(np.diff(S_norm, axis=1) ** 2, axis=0))
    flux = np.concatenate([[0.0], flux])
    return uniform_filter1d(flux, size=_SMOOTH)


def _pitch_contour(waveform: np.ndarray) -> dict:
    """Pitch tracking via pyin. Returns pitch in Hz and pitch direction."""
    f0, voiced, _ = librosa.pyin(
        waveform, fmin=60, fmax=2000,
        sr=SR, hop_length=HOP_LENGTH,
    )
    # fill unvoiced with 0
    f0 = np.where(np.isnan(f0), 0.0, f0)
    voiced = voiced.astype(float)

    # pitch direction: positive = rising, negative = falling (in semitones/frame)
    f0_safe = np.where(f0 > 0, f0, 1.0)
    log_f0 = np.log2(f0_safe)
    direction = np.gradient(log_f0) * 12  # semitones per frame
    direction = np.where(f0 > 0, direction, 0.0)
    direction = uniform_filter1d(direction, size=_SMOOTH)

    return {
        "pitch_hz": f0.tolist(),
        "voiced": voiced.tolist(),
        "pitch_direction": direction.tolist(),
    }


def _phrase_boundaries(rms: np.ndarray) -> list:
    """Detect phrase-level dips in energy (breathing points)."""
    # smooth heavily then find local minima
    smoothed = uniform_filter1d(rms, size=_SMOOTH * 8)
    # local minima where energy dips below the local mean
    local_mean = uniform_filter1d(smoothed, size=_SMOOTH * 20)
    is_dip = (smoothed < local_mean * 0.7)
    # find edges of dip regions — take the center of each
    diffs = np.diff(is_dip.astype(int))
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]
    if len(starts) == 0:
        return []
    if len(ends) == 0 or ends[0] < starts[0]:
        ends = np.append(ends, len(rms) - 1)
    n = min(len(starts), len(ends))
    centers = ((starts[:n] + ends[:n]) / 2).astype(int)
    return centers.tolist()


def _band_split_envelopes(wave: np.ndarray, bands: dict) -> dict:
    """Filter `wave` into each named band, compute peak-normalised
    onset envelope + onset times per band."""
    w = wave.astype(np.float32, copy=False)
    out = {}
    for name, sos in bands.items():
        band = sosfiltfilt(sos, w).astype(np.float32, copy=False)
        onset_env = librosa.onset.onset_strength(
            y=band, sr=SR, hop_length=HOP_LENGTH
        )
        peak = float(onset_env.max()) + 1e-10
        env_norm = (onset_env / peak).astype(np.float32)
        onset_frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=SR, hop_length=HOP_LENGTH,
        )
        out[name] = {
            "onset_envelope": env_norm.tolist(),
            "onset_times": librosa.frames_to_time(
                onset_frames, sr=SR, hop_length=HOP_LENGTH
            ).tolist(),
        }
    return out


def _drum_kit_envelopes(drums_wave: np.ndarray) -> dict:
    """Band-split the drums stem into kick / snare / hat streams."""
    # SR=22050 → nyquist 11025. Keep hat cutoff well below.
    bands = {
        "kick":  butter(4, 120,         btype="lowpass",  fs=SR, output="sos"),
        "snare": butter(4, [180, 1200], btype="bandpass", fs=SR, output="sos"),
        "hat":   butter(4, 7000,        btype="highpass", fs=SR, output="sos"),
    }
    return _band_split_envelopes(drums_wave, bands)


def _bass_register_envelopes(bass_wave: np.ndarray) -> dict:
    """Split the bass stem into sub / mid / high register bands."""
    bands = {
        "sub":  butter(4, 80,         btype="lowpass",  fs=SR, output="sos"),
        "mid":  butter(4, [80, 250],  btype="bandpass", fs=SR, output="sos"),
        "high": butter(4, 250,        btype="highpass", fs=SR, output="sos"),
    }
    return _band_split_envelopes(bass_wave, bands)


def extract_stem_features(waveform: np.ndarray, stem_name: str) -> dict:
    rms = librosa.feature.rms(y=waveform, hop_length=HOP_LENGTH)[0]
    onset_strength = librosa.onset.onset_strength(
        y=waveform, sr=SR, hop_length=HOP_LENGTH
    )

    features = {
        "rms": rms.tolist(),
        "onset_strength": onset_strength.tolist(),
        "spectral_centroid": librosa.feature.spectral_centroid(
            y=waveform, sr=SR, hop_length=HOP_LENGTH
        )[0].tolist(),
        # expressive features
        "dynamic_gradient": _dynamic_gradient(rms).tolist(),
        "attack_envelope": _attack_envelope(onset_strength, rms).tolist(),
        "spectral_flux": _spectral_flux(waveform).tolist(),
    }

    if stem_name in ("vocals", "other"):
        pitch = _pitch_contour(waveform)
        features["pitch_hz"] = pitch["pitch_hz"]
        features["voiced"] = pitch["voiced"]
        features["pitch_direction"] = pitch["pitch_direction"]

    if stem_name == "vocals":
        features["zero_crossing_rate"] = librosa.feature.zero_crossing_rate(
            y=waveform, hop_length=HOP_LENGTH
        )[0].tolist()

    if stem_name == "drums":
        features["kit"] = _drum_kit_envelopes(waveform)

    if stem_name == "bass":
        features["registers"] = _bass_register_envelopes(waveform)

    return features


def extract_macro_features(waveform: np.ndarray) -> dict:
    tempo_arr, beats = librosa.beat.beat_track(
        y=waveform, sr=SR, hop_length=HOP_LENGTH
    )
    tempo = float(np.atleast_1d(tempo_arr)[0])

    mfcc = librosa.feature.mfcc(y=waveform, sr=SR, hop_length=HOP_LENGTH)
    boundaries = librosa.segment.agglomerative(mfcc, k=5)

    key, mode = _estimate_key_mode(waveform)

    rms = librosa.feature.rms(y=waveform, hop_length=HOP_LENGTH)[0]

    return {
        "beats": librosa.frames_to_time(
            beats, sr=SR, hop_length=HOP_LENGTH
        ).tolist(),
        "beat_frames": beats.tolist(),
        "tempo": tempo,
        "section_boundaries": librosa.frames_to_time(
            boundaries, sr=SR, hop_length=HOP_LENGTH
        ).tolist(),
        "section_boundary_frames": boundaries.tolist(),
        "section_labels": [chr(65 + i) for i in range(len(boundaries))],
        "key": key,
        "mode": mode,
        "energy_envelope": rms.tolist(),
        # expressive macro features
        "dynamic_gradient": _dynamic_gradient(rms).tolist(),
        "spectral_flux": _spectral_flux(waveform).tolist(),
        "phrase_boundaries": _phrase_boundaries(rms),
        "phrase_boundary_times": librosa.frames_to_time(
            _phrase_boundaries(rms), sr=SR, hop_length=HOP_LENGTH
        ).tolist(),
    }


def extract_all(stems: dict[str, np.ndarray], mix: np.ndarray) -> dict:
    return {
        "stems": {
            name: extract_stem_features(stems[name], name)
            for name in STEM_NAMES
        },
        "macro": extract_macro_features(mix),
    }
