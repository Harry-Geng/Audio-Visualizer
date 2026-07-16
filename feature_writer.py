import json
import os
from datetime import datetime, timezone


def write_features(features: dict, audio_path: str, output_dir: str | None = None) -> str:
    # verbatim id: the old lower/space→underscore slug was non-injective, so two
    # distinct song ids ("A B" vs "A_B") silently shared one features file.
    # Readers fall back to the legacy slug for pre-existing libraries.
    basename = os.path.splitext(os.path.basename(audio_path))[0]
    filename = f"{basename}_features.json"

    dest_dir = output_dir or os.path.dirname(os.path.abspath(audio_path))
    out_path = os.path.join(dest_dir, filename)

    features["meta"]["processed_at"] = datetime.now(timezone.utc).isoformat()

    with open(out_path, "w") as f:
        json.dump(features, f, separators=(",", ":"))

    return out_path
