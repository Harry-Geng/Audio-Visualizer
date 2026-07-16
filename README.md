# 🎧 Music Microscope

Pull any song apart into its **stems** — vocals, bass, the individual drums of the
kit, melody — and *see* and *explore* it. Everything runs **locally on your own
machine**: your music, your library, nothing uploaded anywhere.

- 🔬 **Stem view** — waveforms + spectrograms of every separated part, solo/mute/mix any of them live
- ✦ **Scene** — reactive visualizers (stage, mandala, pitch-roll, orbs, geometry…)
- ☰ **Lyrics** — word-synced karaoke, plus a full-screen kinetic "Lyric Speaker" view
- ✧ **Similar** — click a moment, find moments *that sound like it* across your whole library
- ✹ **Map** — a starfield of every moment you own, laid out so similar sounds sit together; hover to hear, click to open
- 📻 **Radio** — endless DJ mode: when a song ends it flows into the most similar moment elsewhere in your library
- ⌕ **Text search** — type *"dark moody bassline"* and jump to matching moments

---

## Quick start (Windows)

You need a PC with Python and — ideally — an **NVIDIA GPU** (separation is much
faster on a GPU; it still works on CPU, just slowly).

1. **Install Python 3.12** from [python.org](https://www.python.org/downloads/) —
   tick **"Add Python to PATH"** during install.
2. **Double-click `setup.bat`** — it builds a local environment, installs
   everything (auto-detects your GPU), and grabs `ffmpeg`. Takes a few minutes.
3. **Double-click `run.bat`** — the app opens at
   [http://127.0.0.1:8000](http://127.0.0.1:8000). Leave that window open while
   you use it; close it to stop.

That's it. The first time you analyze a song it downloads the separation models
(a few hundred MB, one time) — so the first song is slow and needs internet.

## Add your music

In the app, click **＋ add music** and either:

- **drop an audio file** (mp3 · flac · wav · m4a) — full quality, or
- **paste a YouTube / SoundCloud link** — it fetches and analyzes it.

Each song is separated once and cached, so it opens instantly forever after.
Options when adding: ✨ HQ vocals, 🥁 drum kit (real kick/snare/hats/…), 🎸 6-stem
(adds guitar + piano). Leave the defaults on for the best experience.

## Power features (optional, after you've built a library)

The per-song views work immediately. Three features scan your **whole** library,
so they need a one-time build step from a terminal in this folder:

```bat
REM the galaxy map (✹ map):
.venv\Scripts\python compute_galaxy.py

REM text-to-sound search (⌕):
.venv\Scripts\python backfill_clap.py

REM synced lyrics for songs added before you cared about lyrics (☰):
.venv\Scripts\python backfill_lyrics.py
```

Re-run `compute_galaxy.py` / `backfill_clap.py` after adding a batch of new songs
to fold them in. (✧ similar and 📻 radio build their index automatically when the
server starts — no step needed.)

## Show friends your library (temporary link)

**Double-click `share.bat`** — it starts a **read-only** copy of the app on your
full library and opens a free Cloudflare quick tunnel, printing an
`https://….trycloudflare.com` link you can send to anyone. Friends get the whole
experience (galaxy, radio, taste, search) but can't add music. Close the window
to stop sharing. Anyone with the link can listen while it's up, so run it only
while you're showing people. Needs `cloudflared` (`winget install
Cloudflare.cloudflared`, or drop `cloudflared.exe` next to the script).

## Where your music library lives

By default everything is stored **inside this folder**. If your collection gets
big (stems are ~130 MB/song), point it at another drive before adding music:

```bat
setx AV_LIBRARY_DIR "E:\microscope_library"
```

Open a new terminal after `setx` for it to take effect.

---

## Manual install (macOS / Linux / no NVIDIA GPU)

```bash
py -3.12 -m venv .venv          # or: python3.12 -m venv .venv
.venv/Scripts/python -m pip install -U pip      # Windows
# source .venv/bin/activate && pip install -U pip   # macOS/Linux

# NVIDIA GPU (Windows):
.venv/Scripts/python -m pip install -r requirements-cuda.txt
# CPU / Apple Silicon:
pip install -r requirements.txt

python microscope.py            # then open http://127.0.0.1:8000
```

You also need **ffmpeg** on your PATH (`winget install Gyan.FFmpeg`,
`brew install ffmpeg`, or `apt install ffmpeg`).

## Bulk-importing a playlist (advanced)

`batch_spotify.py` processes a whole playlist overnight from an
[Exportify](https://exportify.net) CSV or a plain `Artist - Title` text file:

```bash
python batch_spotify.py --from-file myplaylist.csv
```

`retry_missing.py` re-attempts any downloads that failed (with a more resilient
multi-candidate search). See the top of each file for details. These are optional
— most people just add songs through the app.

## Privacy

100% local. The app is a small web server bound to `127.0.0.1` (your machine
only). Your audio, your separated stems, and your library never leave your
computer — this is like running the separation tools at home, because it is.

## License

Code is [MIT licensed](LICENSE) — use, modify, and share it freely. Note that
the *music* you analyze is your own responsibility: keep it to personal use and
respect the rights of the works you download or import.

## Troubleshooting

- **"No module named numpy" / app won't start** — run `setup.bat` first (or you
  ran the system Python instead of `.venv`).
- **Adding a file/link fails** — `ffmpeg` isn't on PATH. Install it, then open a
  **new** terminal window (or re-run `setup.bat`).
- **Separation is very slow** — you're on CPU. A CUDA-capable NVIDIA GPU is ~10–20×
  faster.
- **Port 8000 already in use** — something else is on that port; stop it, or run
  `python microscope.py --port 8010`.
