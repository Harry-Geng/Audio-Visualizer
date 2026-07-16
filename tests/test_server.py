"""HTTP server integration — boots the real handler (demo mode, temp library)
on an ephemeral port and exercises the read-only API surface."""

import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import ThreadingHTTPServer

import pytest

import microscope


@pytest.fixture(scope="module")
def server(demo_song):
    microscope._SONGS = microscope.discover_songs()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), microscope.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(base, path, timeout=15):
    return urllib.request.urlopen(base + path, timeout=timeout)


def _get_json(base, path):
    with _get(base, path) as r:
        return r.status, json.loads(r.read())


def test_appinfo_reports_demo(server):
    status, d = _get_json(server, "/api/appinfo")
    assert status == 200
    assert d["demo"] is True and d["songs"] == 1


def test_songs_lists_the_demo_song(server, demo_song):
    _, songs = _get_json(server, "/api/songs")
    assert [s["id"] for s in songs] == [demo_song]


def test_clip_returns_a_valid_wav_slice(server, demo_song):
    q = urllib.parse.quote(demo_song)
    with _get(server, f"/api/clip?id={q}&start=0.5&end=1.5") as r:
        body = r.read()
    assert body[:4] == b"RIFF"
    with wave.open(io.BytesIO(body)) as w:
        dur = w.getnframes() / w.getframerate()
    assert dur == pytest.approx(1.0, abs=0.05)


def test_clip_clamps_end_past_song_duration(server, demo_song):
    q = urllib.parse.quote(demo_song)
    with _get(server, f"/api/clip?id={q}&start=2.0&end=60.0") as r:
        body = r.read()
    with wave.open(io.BytesIO(body)) as w:
        dur = w.getnframes() / w.getframerate()
    assert dur <= 1.1                      # 3s song → at most ~1s remains


def test_clip_unknown_song_404s(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server, "/api/clip?id=nope&start=0&end=1")
    assert e.value.code == 404


def test_ingest_disabled_in_demo(server):
    req = urllib.request.Request(
        server + "/api/ingest_url", method="POST",
        data=json.dumps({"url": "https://example.com/x"}).encode(),
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=15)
    assert e.value.code == 403


def test_static_path_traversal_is_blocked(server):
    # must never serve files outside microscope_static/
    for probe in ("/static/../microscope.py", "/static/..%2Fmicroscope.py"):
        try:
            with _get(server, probe) as r:
                body = r.read()
                assert b"ThreadingHTTPServer" not in body, f"traversal leaked via {probe}"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 403, 404)


def test_similar_reports_building_then_202_or_ready(server, demo_song):
    q = urllib.parse.quote(demo_song)
    try:
        with _get(server, f"/api/similar?id={q}&t=1.0&facet=mix&k=3") as r:
            assert r.status in (200, 202)
    except urllib.error.HTTPError as e:      # no moments.npz in this library
        assert e.code in (202, 404)
