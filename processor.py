import os
import numpy as np
import librosa
import soundfile as sf

from config import SR, HOP_LENGTH, FPS, DEMUCS_MODEL
from stem_separator import separate_stems
from feature_extractor import extract_all
from feature_writer import write_features


def process(audio_path: str, output_dir: str | None = None, save_stems: bool = False) -> str:
    print(f"Loading {audio_path}...")
    mix, _ = librosa.load(audio_path, sr=SR, mono=True)

    print("Separating stems (this takes a while)...")
    stems = separate_stems(audio_path)

    if save_stems:
        base = os.path.splitext(os.path.basename(audio_path))[0]
        stems_dir = os.path.join(output_dir or os.path.dirname(audio_path), f"{base}_stems")
        os.makedirs(stems_dir, exist_ok=True)
        for name, audio in stems.items():
            out = os.path.join(stems_dir, f"{name}.wav")
            sf.write(out, audio, SR)
            print(f"  Saved stem: {out}")

    print("Extracting features...")
    features = extract_all(stems, mix)

    n_frames = len(features["macro"]["energy_envelope"])
    duration = librosa.get_duration(y=mix, sr=SR)

    features["meta"] = {
        "filename": os.path.basename(audio_path),
        "duration_seconds": round(duration, 6),
        "sample_rate": SR,
        "hop_length": HOP_LENGTH,
        "fps": round(FPS, 6),
        "n_frames": n_frames,
        "processed_at": "",  # filled by writer
    }

    print("Writing JSON...")
    out_path = write_features(features, audio_path, output_dir)
    print(f"Done: {out_path}")
    return out_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python processor.py <audio_file> [output_dir] [--save-stems]")
        sys.exit(1)

    audio = sys.argv[1]
    out = None
    save_stems = False
    for arg in sys.argv[2:]:
        if arg == "--save-stems":
            save_stems = True
        else:
            out = arg
    process(audio, out, save_stems)
