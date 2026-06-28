import os

SR = 22050
HOP_LENGTH = 512
FPS = SR / HOP_LENGTH          # ~43.07
DEMUCS_MODEL = "htdemucs"
STEM_NAMES = ["drums", "bass", "vocals", "other"]

# Root that holds the song library: <id>_stems/, <id>_stems_hq/, <id>.flac,
# *_features.json, *_moments.npz ... Defaults to the project dir so existing
# songs keep working unchanged. Point AV_LIBRARY_DIR at an external drive for
# the big batch (900 songs ~= 350-400 GB).
LIBRARY_DIR = os.path.abspath(
    os.environ.get("AV_LIBRARY_DIR")
    or os.path.dirname(os.path.abspath(__file__))
)
os.makedirs(LIBRARY_DIR, exist_ok=True)

# Stems are stored as FLAC (lossless, ~4x smaller than PCM WAV). Older songs may
# still be WAV, so readers resolve a stem by name across both, preferring FLAC.
STEM_EXTS = (".flac", ".wav")


def stem_file(stems_dir, name):
    """Return the path to stem `name` in `stems_dir` (FLAC preferred), or None."""
    for ext in STEM_EXTS:
        p = os.path.join(stems_dir, name + ext)
        if os.path.exists(p):
            return p
    return None
