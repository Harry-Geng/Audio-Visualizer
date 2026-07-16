"""Batch/retry parsing helpers and cross-module slug contracts.

Marked `heavy`: importing batch_spotify/ingest pulls in torch + demucs
(one-time ~15 s import, no models loaded). Skip with:  pytest -m "not heavy"
"""

import os

import pytest

pytestmark = pytest.mark.heavy


# ---------------------------------------------------------------- batch parsing
def test_dur_to_seconds_variants():
    import batch_spotify as bs
    assert bs._dur_to_seconds("3:45") == 225
    assert bs._dur_to_seconds("225") == 225.0
    assert bs._dur_to_seconds("225000") == 225.0          # bare ms magnitude
    assert bs._dur_to_seconds("198000", is_ms=True) == 198.0
    assert bs._dur_to_seconds("") == 0.0
    assert bs._dur_to_seconds(None) == 0.0


def test_load_tracks_txt(tmp_path):
    import batch_spotify as bs
    p = tmp_path / "list.txt"
    p.write_text("Artist One - Song A\nJustATitle\n\n", encoding="utf-8")
    tracks = bs.load_tracks_from_file(str(p))
    assert tracks[0]["artist"] == "Artist One" and tracks[0]["name"] == "Song A"
    assert tracks[1]["artist"] == "" and tracks[1]["name"] == "JustATitle"


def test_load_tracks_exportify_csv(tmp_path):
    import batch_spotify as bs
    p = tmp_path / "export.csv"
    p.write_text(
        'Track Name,Artist Name(s),Duration (ms)\n'
        'Song A,"First; Second",198000\n',
        encoding="utf-8",
    )
    (t,) = bs.load_tracks_from_file(str(p))
    assert t["title"] == "First, Second - Song A"
    assert t["duration_s"] == pytest.approx(198.0)


def test_query_variants_dedup_and_order():
    import retry_missing as rm
    v = rm._query_variants("Artist - Song (feat. Guest)")
    assert v[0] == "Artist - Song (feat. Guest)"          # exact first
    assert "Artist - Song audio" in v                     # stripped variant
    assert len(v) == len(set(v))                          # no duplicates


# ------------------------------------------------------------------ slug contracts
def test_base_slug_strips_and_truncates():
    import ingest
    assert ingest._base_slug("A/B\\C:D*E?F") == "ABCDEF"
    assert len(ingest._base_slug("x" * 200)) == 80
    assert ingest._base_slug("...") == "..."              # dots survive


def test_features_slug_roundtrip(tmp_path):
    """write_features(<id>.flac) must land where microscope._features_path looks."""
    import microscope
    from feature_writer import write_features
    from config import LIBRARY_DIR

    for sid in ["Plain Song", "Dots.And...Dots", "Tory Lanez - Dont Walk Away...Just Trust Me"]:
        feats = {"meta": {}}
        out = write_features(feats, os.path.join(LIBRARY_DIR, f"{sid}.flac"))
        try:
            assert microscope._features_path(sid) == out
        finally:
            os.remove(out)
