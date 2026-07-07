"""
Batch-preprocess a Spotify playlist into the Music Microscope library.

Pipeline per track:
  Spotify Web API (titles/artists)  ->  yt-dlp search + download (duration-matched)
  ->  the standard ingest pipeline at the bulk tier (HQ vocals + DrumSep kit,
      no guitar/piano split)  ->  stems + features + moment index.

Designed for an overnight run over ~900 songs on one GPU:
  - All track metadata is fetched UP FRONT (seconds), so the Spotify token never
    needs refreshing during the long separation run.
  - RESUMABLE / idempotent: a track whose <id>_stems/ already exists is skipped,
    so you can stop and restart freely.
  - Every track's outcome is appended to batch_log.jsonl in the library dir.

Setup:
  1. Create a Spotify app at https://developer.spotify.com/dashboard, copy its
     Client ID + Client Secret, and register the redirect URI EXACTLY:
        http://127.0.0.1:8080/callback
     (Spotify now requires a user-authorized token to read playlist items, so the
     plain Client-Credentials flow no longer works — hence the one-time login.)
  2. Provide the creds via env vars  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
     or a spotify_creds.json file ({"client_id": "...", "client_secret": "..."}).
  3. The first run opens a browser to approve your own app; the token is then
     cached in spotify_token.json and auto-refreshed.
  4. (optional) Point the library at an external drive:
        setx AV_LIBRARY_DIR "E:\\microscope_library"     (new shell after this)

Track source — Spotify disabled the playlist /tracks endpoint for new apps, so
the reliable path is a one-time export:
  1. Go to https://exportify.net, log in, export the playlist to CSV.
  2. python batch_spotify.py --from-file <export.csv>
The Spotify-API path (passing a playlist URL) still works for older/whitelisted
apps and is kept as a fallback.

Usage:
  python batch_spotify.py --from-file export.csv             # from an Exportify CSV
  python batch_spotify.py --from-file export.csv --limit 20  # validate on 20 first
  python batch_spotify.py --from-file export.csv --dry-run   # list tracks, do nothing
  python batch_spotify.py <playlist-url-or-id>               # API fallback
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import secrets
import argparse
import tempfile
import traceback
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# Track titles routinely contain accented characters; the default Windows
# console (cp1252) raises UnicodeEncodeError on them. Force UTF-8 output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import LIBRARY_DIR
import ingest

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(LIBRARY_DIR, "batch_log.jsonl")
TOKEN_PATH = os.path.join(HERE, "spotify_token.json")
DUR_TOLERANCE_S = 25          # accept a YouTube hit within +-25s of Spotify length
SEARCH_N = 4                  # candidates to fetch per track, best-duration wins

# Spotify now refuses playlist-item reads from app-only (Client-Credentials)
# tokens, so we use the Authorization-Code flow: a one-time browser login grants
# a user-scoped token, then we cache + refresh it.
REDIRECT_URI = "http://127.0.0.1:8080/callback"   # MUST match the app's settings
AUTH_SCOPE = "playlist-read-private playlist-read-collaborative"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def load_credentials(cli_id=None, cli_secret=None):
    cid = cli_id or os.environ.get("SPOTIFY_CLIENT_ID")
    sec = cli_secret or os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and sec):
        path = os.path.join(HERE, "spotify_creds.json")
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            cid = cid or d.get("client_id")
            sec = sec or d.get("client_secret")
    if not (cid and sec):
        sys.exit(
            "Missing Spotify credentials. Set SPOTIFY_CLIENT_ID / "
            "SPOTIFY_CLIENT_SECRET, pass --client-id/--client-secret, or create "
            "spotify_creds.json. Get them at https://developer.spotify.com/dashboard"
        )
    return cid, sec


# ---------------------------------------------------------------------------
# Spotify auth — Authorization-Code flow with a one-shot local callback server.
# Token (+ refresh token) is cached so the browser login happens only once.
# ---------------------------------------------------------------------------
def _save_token(tok):
    refresh = tok.get("refresh_token")
    if not refresh and os.path.exists(TOKEN_PATH):      # refresh responses omit it
        try:
            refresh = json.load(open(TOKEN_PATH)).get("refresh_token")
        except Exception:
            pass
    data = {"access_token": tok["access_token"], "refresh_token": refresh,
            "expires_at": time.time() + tok.get("expires_in", 3600) - 60}
    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f)
    return data["access_token"]


def _exchange_code(cid, sec, code):
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": REDIRECT_URI},
        auth=(cid, sec), timeout=30,
    )
    r.raise_for_status()
    return _save_token(r.json())


def _refresh_token(cid, sec, refresh):
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        auth=(cid, sec), timeout=30,
    )
    r.raise_for_status()
    return _save_token(r.json())


def _browser_login(cid, sec):
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": AUTH_SCOPE, "state": state,
    })
    box = {}

    class _Cb(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            box["code"] = qs.get("code", [None])[0]
            box["state"] = qs.get("state", [None])[0]
            box["error"] = qs.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = box.get("code") and not box.get("error")
            self.wfile.write(
                b"<h2>Authorized - close this tab and return to the terminal.</h2>"
                if ok else b"<h2>Authorization failed. Check the terminal.</h2>")

    try:
        srv = HTTPServer(("127.0.0.1", 8080), _Cb)
    except OSError as e:
        sys.exit(f"Could not start local callback server on :8080 ({e}). "
                 "Close whatever is using port 8080 and retry.")
    print("[auth] opening browser for a one-time Spotify login…")
    print(f"       if it doesn't open, paste this into your browser:\n       {auth_url}")
    webbrowser.open(auth_url)
    while "code" not in box and "error" not in box:     # ignore favicon etc.
        srv.handle_request()
    srv.server_close()

    if box.get("error"):
        sys.exit(f"Spotify authorization failed: {box['error']}")
    if box.get("state") != state:
        sys.exit("Spotify auth state mismatch — aborting.")
    if not box.get("code"):
        sys.exit("No authorization code received from Spotify.")
    return _exchange_code(cid, sec, box["code"])


def get_user_token(cid, sec):
    """Return a user-scoped access token, using the cache/refresh when possible."""
    if os.path.exists(TOKEN_PATH):
        try:
            t = json.load(open(TOKEN_PATH))
            if t.get("access_token") and t.get("expires_at", 0) > time.time():
                return t["access_token"]
            if t.get("refresh_token"):
                return _refresh_token(cid, sec, t["refresh_token"])
        except Exception:
            pass
    return _browser_login(cid, sec)


def parse_playlist_id(arg):
    """Accept a raw id, spotify:playlist:ID, or an open.spotify.com URL."""
    m = re.search(r"playlist[:/]([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg.strip()


# ---------------------------------------------------------------------------
# Track lists from a file — the reliable path. Spotify disabled the playlist
# /tracks endpoint for new apps, so export once (Exportify -> CSV) and read that.
# ---------------------------------------------------------------------------
_NAME_KEYS = ["track name", "title", "name", "song", "track"]
_ARTIST_KEYS = ["artist name(s)", "artist name", "artists", "artist(s)", "artist"]
_DUR_MS_KEYS = ["duration (ms)", "track duration (ms)", "duration_ms", "duration ms"]
_DUR_KEYS = ["duration", "length", "time"]


def _pick(row_low, keys):
    for k in keys:
        if row_low.get(k):
            return row_low[k]
    return None


def _dur_to_seconds(val, is_ms=False):
    val = (val or "").strip()
    if not val:
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", val):
        n = float(val)
        if is_ms or n > 1000:        # ms columns, or a bare value too big for seconds
            return n / 1000.0
        return n
    m = re.fullmatch(r"(\d+):(\d{1,2})", val)   # m:ss
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0.0


def load_tracks_from_file(path):
    """Read tracks from an Exportify-style CSV or a 'Artist - Title' per-line TXT."""
    if not os.path.exists(path):
        sys.exit(f"Track file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    tracks = []

    if ext in (".txt", ""):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                artist, name = (line.split(" - ", 1) + [""])[:2] if " - " in line \
                    else ("", line)
                artist, name = artist.strip(), name.strip()
                tracks.append({"title": line, "artist": artist, "name": name,
                               "spotify_id": None, "duration_s": 0.0})
        return tracks

    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            low = {(k or "").strip().lower(): v for k, v in row.items()}
            name = _pick(low, _NAME_KEYS)
            if not name:
                continue
            artist = (_pick(low, _ARTIST_KEYS) or "").strip()
            if ";" in artist:        # Exportify separates multiple artists with ;
                artist = ", ".join(a.strip() for a in artist.split(";") if a.strip())
            ms = _pick(low, _DUR_MS_KEYS)
            dur_s = _dur_to_seconds(ms, is_ms=True) if ms \
                else _dur_to_seconds(_pick(low, _DUR_KEYS))
            name = name.strip()
            title = f"{artist} - {name}" if artist else name
            tracks.append({"title": title, "artist": artist, "name": name,
                           "spotify_id": None, "duration_s": dur_s})
    return tracks


def fetch_playlist_tracks(playlist_id, token):
    """Return [{title, artist, name, spotify_id, duration_s}] for the playlist."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {
        "limit": 100,
        "fields": "next,items(track(name,artists(name),id,duration_ms,is_local))",
    }
    tracks = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 404:
            sys.exit(f"Playlist {playlist_id} not found (is it public?).")
        if r.status_code == 403:
            sys.exit(
                "Spotify returned 403 for the playlist /tracks endpoint. Spotify "
                "has disabled this endpoint for newly-created apps, and it can't be "
                "re-enabled on your end.\nWorkaround: export the playlist at "
                "https://exportify.net (log in, download CSV), then run:\n"
                "    python batch_spotify.py --from-file <export.csv>")
        r.raise_for_status()
        data = r.json()
        for it in data.get("items", []):
            t = it.get("track")
            if not t or t.get("is_local"):
                continue
            name = t.get("name")
            if not name:
                continue
            artist = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
            tracks.append({
                "title": f"{artist} - {name}" if artist else name,
                "artist": artist, "name": name,
                "spotify_id": t.get("id"),
                "duration_s": (t.get("duration_ms") or 0) / 1000.0,
            })
        url = data.get("next")     # already a fully-formed URL with paging
        params = None
    return tracks


# ---------------------------------------------------------------------------
# YouTube download (yt-dlp), choosing the candidate closest in length
# ---------------------------------------------------------------------------
def download_track(query, target_s, tmpdir, search_n=SEARCH_N, max_tries=3):
    """Search YouTube, pick the best-duration match, download + extract to FLAC.

    Tries successive candidates (best-duration first) when a pick can't be
    downloaded — a single dead / geo-blocked / "video not available" upload no
    longer kills the whole track, which is the main reason batch downloads fail.

    Returns (flac_path, picked_meta). Raises only if every candidate fails.
    """
    import yt_dlp

    # Optional: pull cookies from a logged-in browser (AV_COOKIES_BROWSER=brave|chrome|
    # edge|firefox…) to get past age-gates and most bot-checks on retry passes.
    _cb = os.environ.get("AV_COOKIES_BROWSER")
    cookie_opt = {"cookiesfrombrowser": (_cb,)} if _cb else {}

    search_opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                   "skip_download": True, **cookie_opt}
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{search_n}:{query}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise RuntimeError("no YouTube results")

    if target_s:
        entries.sort(key=lambda e: abs((e.get("duration") or 0) - target_s))

    dl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "dl.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}],
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        **cookie_opt,
    }

    errors = []
    for cand in entries[:max_tries]:
        page = cand.get("webpage_url") or cand.get("original_url") or cand.get("url")
        for f in os.listdir(tmpdir):                     # clear a prior attempt's partial
            if f.startswith("dl."):
                try:
                    os.remove(os.path.join(tmpdir, f))
                except OSError:
                    pass
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([page])
        except Exception as e:
            errors.append(f"{cand.get('id')}: {str(e).splitlines()[-1][:80]}")
            continue                                     # dead upload → next candidate

        flac = os.path.join(tmpdir, "dl.flac")
        if not os.path.exists(flac):
            cands = [f for f in os.listdir(tmpdir) if f.startswith("dl.")]
            if not cands:
                errors.append(f"{cand.get('id')}: produced no file")
                continue
            flac = os.path.join(tmpdir, cands[0])

        dur = cand.get("duration")
        far = bool(target_s and dur and abs(dur - target_s) > DUR_TOLERANCE_S)
        return flac, {
            "yt_title": cand.get("title"), "yt_id": cand.get("id"),
            "yt_duration": dur, "duration_mismatch": far,
        }

    raise RuntimeError("all candidates failed — " + " | ".join(errors[:4]))


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def log_result(rec):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _fmt_dur(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Batch-process a Spotify playlist.")
    ap.add_argument("playlist", nargs="?",
                    help="Spotify playlist URL or id (omit when using --from-file)")
    ap.add_argument("--from-file", default=None,
                    help="read tracks from an Exportify CSV or 'Artist - Title' TXT "
                         "instead of the Spotify API (the reliable path)")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N new tracks (0 = all)")
    ap.add_argument("--start", type=int, default=0,
                    help="skip the first N tracks of the list")
    ap.add_argument("--dry-run", action="store_true",
                    help="list tracks and exit (no download / processing)")
    ap.add_argument("--client-id", default=None)
    ap.add_argument("--client-secret", default=None)
    args = ap.parse_args()

    print(f"[batch] library dir: {LIBRARY_DIR}")
    if args.from_file:
        tracks = load_tracks_from_file(args.from_file)
        print(f"[batch] {len(tracks)} tracks loaded from {args.from_file}")
    else:
        if not args.playlist:
            ap.error("give a playlist URL/id, or use --from-file <export.csv>")
        pid = parse_playlist_id(args.playlist)
        cid, sec = load_credentials(args.client_id, args.client_secret)
        token = get_user_token(cid, sec)
        print(f"[batch] fetching playlist {pid} …")
        tracks = fetch_playlist_tracks(pid, token)
        print(f"[batch] {len(tracks)} tracks in playlist")

    if args.start:
        tracks = tracks[args.start:]

    if args.dry_run:
        for i, t in enumerate(tracks, 1):
            slug = ingest._base_slug(t["title"])
            done = os.path.isdir(os.path.join(LIBRARY_DIR, f"{slug}_stems"))
            mark = "[done]" if done else "      "
            print(f"  {i:3d}. {mark} {t['title']}  ({_fmt_dur(t['duration_s'])})")
        return

    processed = skipped = failed = 0
    t_start = time.time()

    # ── producer/consumer: a background thread downloads the next songs (yt-dlp is
    #    network/CPU-bound) while the main thread runs separation on the GPU, so the
    #    GPU never idles waiting on a download. PREFETCH bounds how far ahead we pull
    #    (and thus temp-disk use). Only the main thread touches the GPU pipeline. ──
    PREFETCH = 2
    dlq = queue.Queue(maxsize=PREFETCH)
    stop_evt = threading.Event()

    def _tmpdir_of(item):
        return item[5] if item[0] == "ok" else item[4]

    def producer():
        queued = 0
        for idx, tr in enumerate(tracks, 1):
            if stop_evt.is_set():
                break
            if args.limit and queued >= args.limit:
                break
            slug = ingest._base_slug(tr["title"])
            if os.path.isdir(os.path.join(LIBRARY_DIR, f"{slug}_stems")):
                continue                                   # already done → skip silently
            queued += 1
            tmpdir = tempfile.mkdtemp(prefix="batch_")
            try:
                flac, meta = download_track(tr["title"], tr["duration_s"], tmpdir)
                item = ("ok", idx, tr, flac, meta, tmpdir)
            except Exception as e:                         # download failure → report
                item = ("dlfail", idx, tr, e, tmpdir)
            while not stop_evt.is_set():                   # block when consumer is behind
                try:
                    dlq.put(item, timeout=1.0); break
                except queue.Full:
                    continue
            else:
                shutil.rmtree(tmpdir, ignore_errors=True); return
        dlq.put(None)                                      # sentinel: no more songs

    prod = threading.Thread(target=producer, name="downloader", daemon=True)
    prod.start()

    try:
        while True:
            item = dlq.get()
            if item is None:
                break
            t0 = time.time()
            n_new = processed + failed + 1
            eta = ""
            if processed + failed:
                avg = (time.time() - t_start) / (processed + failed)
                eta = f" | ~{_fmt_dur(avg)}/song"
            idx, tr = item[1], item[2]
            print(f"\n[{idx}/{len(tracks)}] (#{n_new} new){eta}  {tr['title']}")

            if item[0] == "dlfail":
                failed += 1
                e = item[3]
                print(f"    FAILED (download): {e}")
                log_result({"title": tr["title"], "spotify_id": tr["spotify_id"],
                            "status": "error", "error": str(e),
                            "elapsed_s": round(time.time() - t0, 1)})
                shutil.rmtree(item[4], ignore_errors=True)
                continue

            _, _, _, flac, meta, tmpdir = item
            if meta.get("duration_mismatch"):
                print(f"    ! duration mismatch: yt={_fmt_dur(meta['yt_duration'])} "
                      f"vs spotify={_fmt_dur(tr['duration_s'])} — '{meta['yt_title']}'")
            try:
                job = ingest.process_file_sync(
                    flac, tr["title"], hq_vocals=True, drum_kit=True, lossy=True)
                rec = {"title": tr["title"], "spotify_id": tr["spotify_id"],
                       "song_id": job.song_id, "elapsed_s": round(time.time() - t0, 1),
                       **meta}
                if job.error:
                    failed += 1
                    rec["status"] = "error"; rec["error"] = job.error
                    print(f"    FAILED: {job.error}")
                else:
                    processed += 1
                    rec["status"] = "ok"
                    print(f"    ok -> {job.song_id}  ({rec['elapsed_s']}s)")
                log_result(rec)
            except Exception as e:
                failed += 1
                print(f"    FAILED (pipeline): {e}")
                traceback.print_exc()
                log_result({"title": tr["title"], "spotify_id": tr["spotify_id"],
                            "status": "error", "error": str(e),
                            "elapsed_s": round(time.time() - t0, 1)})
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

            if args.limit and processed >= args.limit:
                print(f"[batch] reached --limit {args.limit}, stopping.")
                break
    except KeyboardInterrupt:
        print("\n[batch] interrupted by user.")
    finally:
        stop_evt.set()
        try:                                               # clean up prefetched temp dirs
            while True:
                left = dlq.get_nowait()
                if left:
                    shutil.rmtree(_tmpdir_of(left), ignore_errors=True)
        except queue.Empty:
            pass

    dt = time.time() - t_start
    print(f"\n[batch] done in {_fmt_dur(dt)}  |  "
          f"processed {processed}, skipped {skipped}, failed {failed}")
    print(f"[batch] log: {LOG_PATH}")


if __name__ == "__main__":
    main()
