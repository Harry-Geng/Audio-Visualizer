import json
import os
from datetime import datetime, timezone


def write_features(features: dict, audio_path: str, output_dir: str | None = None) -> str:
    basename = os.path.splitext(os.path.basename(audio_path))[0]
    slug = basename.lower().replace(" ", "_")
    filename = f"{slug}_features.json"

    dest_dir = output_dir or os.path.dirname(os.path.abspath(audio_path))
    out_path = os.path.join(dest_dir, filename)

    features["meta"]["processed_at"] = datetime.now(timezone.utc).isoformat()

    with open(out_path, "w") as f:
        json.dump(features, f, separators=(",", ":"))

    return out_path
