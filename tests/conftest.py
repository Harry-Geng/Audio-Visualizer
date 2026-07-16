"""Test bootstrap.

config.py reads AV_LIBRARY_DIR (and microscope.py reads AV_DEMO) AT IMPORT TIME,
so the environment must be pinned before any project module is imported — which
is why this happens at conftest module level, not inside a fixture.

AV_LIBRARY_DIR is HARD-overridden to a temp dir so no test can ever touch a real
library. AV_DEMO=1 makes `import microscope` skip the heavy ingest/demucs stack
(tests that need the real ingest import it explicitly and are marked `heavy`).
"""

import os
import sys
import shutil
import tempfile

_TMP_LIB = tempfile.mkdtemp(prefix="av_testlib_")
os.environ["AV_LIBRARY_DIR"] = _TMP_LIB          # override, never the real library
os.environ["AV_DEMO"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np      # noqa: E402
import pytest           # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_LIB, ignore_errors=True)


@pytest.fixture(scope="session")
def library_dir():
    """The temp library dir every project module sees as LIBRARY_DIR."""
    return _TMP_LIB


@pytest.fixture(scope="session")
def demo_song(library_dir):
    """Synthesize one complete song in the temp library: 4 analysis stems
    (22.05 kHz mono FLAC sines) + a stereo 44.1 kHz original. Returns its id."""
    import soundfile as sf

    sid = "Test Artist - Tiny Song"
    sr_an, dur_s = 22050, 3.0
    t = np.arange(int(sr_an * dur_s)) / sr_an
    stems_dir = os.path.join(library_dir, f"{sid}_stems")
    os.makedirs(stems_dir, exist_ok=True)
    for name, hz in [("drums", 110.0), ("bass", 55.0),
                     ("vocals", 440.0), ("other", 220.0)]:
        y = (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)
        sf.write(os.path.join(stems_dir, f"{name}.flac"), y, sr_an, subtype="PCM_16")

    sr_full = 44100
    tf = np.arange(int(sr_full * dur_s)) / sr_full
    orig = (0.3 * np.sin(2 * np.pi * 220.0 * tf)).astype(np.float32)
    sf.write(os.path.join(library_dir, f"{sid}.flac"),
             np.stack([orig, orig], axis=1), sr_full, subtype="PCM_16")
    return sid
