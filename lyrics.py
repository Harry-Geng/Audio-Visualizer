"""
Lyrics for the Music Microscope — hybrid official-text + forced-alignment.

Pipeline per song:
  1. fetch official lyrics from LRCLIB (free, no-auth): plain text + line-synced
     LRC timestamps.
  2. forced-align the official WORDS to the isolated vocal stem using torchaudio's
     MMS_FA aligner -> per-word timestamps (karaoke). Alignment runs per LRC line
     within its time window, so it stays robust on repeated choruses / ad-libs.

Output: <id>_lyrics.json
  { song_id, source, synced, matched:{artist,track}, duration,
    lines: [ { t, end, text, words: [ {w, t, end} ... ] } ... ],
    plain }

Whisper is NOT required: since LRCLIB supplies the text, the word-level step is
forced alignment (torchaudio, already installed), not ASR. ASR would only be a
fallback for songs LRCLIB doesn't have.
"""

import os
import re
import json
import threading

import requests
import numpy as np
import soundfile as sf

from config import stem_file, LIBRARY_DIR

LRCLIB = "https://lrclib.net/api"
FA_SR = 16000                      # MMS_FA expects 16 kHz mono
_LEAD_PAD = 0.2                    # widen each line window slightly at the start

_M = None
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# LRCLIB fetch
# ---------------------------------------------------------------------------
_FEAT_RE = re.compile(r"\s*[\(\[](feat|ft|with)\.?\s.*", re.I)


def clean_query(artist, track):
    """Primary artist + track without '(feat. ...)' — better LRCLIB hit rate."""
    artist = (artist or "").split(",")[0].split(";")[0].strip()
    track = _FEAT_RE.sub("", track or "").strip()
    return artist, track


def fetch_lrclib(artist, track, duration=None):
    """Best LRCLIB match (prefers synced lyrics, then closest duration), or None."""
    try:
        r = requests.get(f"{LRCLIB}/search",
                         params={"artist_name": artist, "track_name": track}, timeout=20)
    except Exception:
        return None
    if not r.ok:
        return None
    hits = r.json() or []
    synced = [h for h in hits if h.get("syncedLyrics")]
    pool = synced or hits
    if not pool:
        return None
    if duration:
        pool.sort(key=lambda h: abs((h.get("duration") or 0) - duration))
    return pool[0]


_LRC_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


def parse_lrc(text):
    """LRC string -> [(time_seconds, line_text)] in order."""
    out = []
    for ln in text.splitlines():
        m = _LRC_RE.match(ln)
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


# ---------------------------------------------------------------------------
# forced alignment (torchaudio MMS_FA)
# ---------------------------------------------------------------------------
def _aligner():
    global _M
    with _LOCK:
        if _M is None:
            import torch
            import torchaudio
            bundle = torchaudio.pipelines.MMS_FA
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            _M = {
                "torch": torch, "ta": torchaudio, "dev": dev,
                "model": bundle.get_model().to(dev),
                "tok": bundle.get_tokenizer(),
                "aligner": bundle.get_aligner(),
            }
        return _M


_WORD_RE = re.compile(r"[^a-z']")


def _word_pairs(text):
    """[(display_word, normalized_word)] keeping only alignable tokens."""
    pairs = []
    for tok in text.split():
        norm = _WORD_RE.sub("", tok.lower())
        if norm:
            pairs.append((tok, norm))
    return pairs


def _load_vocal_16k(stems_dir):
    p = stem_file(stems_dir, "vocals")
    if p is None:
        return None
    y, sr = sf.read(p, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != FA_SR:
        M = _aligner()
        y = M["ta"].functional.resample(M["torch"].from_numpy(y), sr, FA_SR).numpy()
    return y


def _align_line(M, wave16, t0, t1, norm_words):
    """Word time spans (absolute seconds) for one line, or None on failure."""
    i0, i1 = max(0, int(t0 * FA_SR)), min(len(wave16), int(t1 * FA_SR))
    if i1 - i0 < FA_SR * 0.1 or not norm_words:
        return None
    seg = M["torch"].from_numpy(wave16[i0:i1]).unsqueeze(0).to(M["dev"])
    with M["torch"].inference_mode():
        emission, _ = M["model"](seg)
    spans = M["aligner"](emission[0], M["tok"](norm_words))
    ratio = seg.size(1) / emission.size(1) / FA_SR
    return [(t0 + s[0].start * ratio, t0 + s[-1].end * ratio) for s in spans]


# ---------------------------------------------------------------------------
# build + persist
# ---------------------------------------------------------------------------
def _write(out_dir, song_id, data):
    path = os.path.join(out_dir, f"{song_id}_lyrics.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def build_song_lyrics(song_id, artist, track, duration, stems_dir,
                      out_dir=None, align=True, verbose=True):
    """Fetch + (optionally) align lyrics for one song; write <id>_lyrics.json."""
    out_dir = out_dir or LIBRARY_DIR
    artist, track = clean_query(artist, track)
    hit = fetch_lrclib(artist, track, duration)
    if not hit:
        if verbose:
            print(f"  [{song_id}] no LRCLIB match")
        data = {"song_id": song_id, "source": "none", "synced": False,
                "lines": [], "plain": ""}
        _write(out_dir, song_id, data)
        return data

    matched = {"artist": hit.get("artistName"), "track": hit.get("trackName")}
    synced = hit.get("syncedLyrics")
    plain = hit.get("plainLyrics") or ""

    if not synced:                                  # unsynced: line text only
        data = {"song_id": song_id, "source": "lrclib", "synced": False,
                "matched": matched, "duration": duration, "plain": plain,
                "lines": [{"t": None, "end": None, "text": l, "words": []}
                          for l in plain.splitlines()]}
        _write(out_dir, song_id, data)
        if verbose:
            print(f"  [{song_id}] LRCLIB plain ({len(data['lines'])} lines, no timing)")
        return data

    parsed = parse_lrc(synced)
    wave16 = _load_vocal_16k(stems_dir) if align else None
    M = _aligner() if wave16 is not None else None

    lines, n_aligned = [], 0
    for i, (t, s) in enumerate(parsed):
        t1 = parsed[i + 1][0] if i + 1 < len(parsed) else (duration or t + 5)
        line = {"t": round(t, 3), "end": round(t1, 3), "text": s, "words": []}
        if s and wave16 is not None:
            pairs = _word_pairs(s)
            if pairs:
                try:
                    spans = _align_line(M, wave16, max(0.0, t - _LEAD_PAD), t1,
                                        [n for _, n in pairs])
                except Exception:
                    spans = None
                if spans and len(spans) == len(pairs):
                    line["words"] = [{"w": d, "t": round(a, 3), "end": round(b, 3)}
                                     for (d, _), (a, b) in zip(pairs, spans)]
                    n_aligned += 1
        lines.append(line)

    source = "lrclib+mms_fa" if wave16 is not None else "lrclib"
    data = {"song_id": song_id, "source": source, "synced": True,
            "matched": matched, "duration": duration, "lines": lines, "plain": plain}
    _write(out_dir, song_id, data)
    if verbose:
        print(f"  [{song_id}] {matched['artist']} - {matched['track']}: "
              f"{len(lines)} lines, {n_aligned} word-aligned ({source})")
    return data
