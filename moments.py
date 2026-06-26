"""
Moment segmentation for the similarity / taste-mapping engine.

A "moment" is the unit of comparison: a beat-aligned window of audio (~5 s).
We segment on beats rather than clock time so a window always spans the same
*musical* length regardless of tempo — a moment at 90 bpm compares fairly to
one at 140 bpm, and window edges land on musical boundaries.

Default window = 8 beats (≈ 2 bars in 4/4), hopping 4 beats → 50 % overlap.
At the project's typical tempos that lands near the ~5 s target, but the size
is defined in beats, not seconds, on purpose.
"""

from dataclasses import dataclass, asdict


@dataclass
class Moment:
    idx: int            # position in the song's moment sequence
    start_t: float      # seconds
    end_t: float        # seconds
    start_beat: int     # index into the beat list
    end_beat: int       # exclusive
    n_beats: int        # beats actually spanned (last window may be short)

    def as_dict(self):
        return asdict(self)


def segment_by_beats(beats, duration,
                     beats_per_window=8, hop_beats=4,
                     min_tail_beats=4):
    """Slice a song into beat-aligned moments.

    Parameters
    ----------
    beats : sequence[float]
        Beat onset times in seconds (e.g. features['macro']['beats']).
    duration : float
        Song length in seconds; bounds the final window.
    beats_per_window : int
        Window length in beats (8 ≈ 2 bars).
    hop_beats : int
        Step between window starts (4 → 50 % overlap with an 8-beat window).
    min_tail_beats : int
        Only emit a final short window if at least this many beats remain,
        so we don't create a sliver moment from one or two trailing beats.

    Returns
    -------
    list[Moment]
    """
    beats = [float(b) for b in beats]
    n = len(beats)
    if n < 2:
        # No usable beat grid — fall back to one moment spanning the whole song.
        return [Moment(0, 0.0, float(duration), 0, 0, 0)]

    moments = []
    i = 0
    idx = 0
    while i < n - 1:
        j = i + beats_per_window
        if j <= n - 1:
            # full window: [beats[i], beats[j])
            start_t, end_t = beats[i], beats[j]
            end_beat = j
        else:
            # tail: from beats[i] to the song end
            remaining = (n - 1) - i
            if idx > 0 and remaining < min_tail_beats:
                break
            start_t, end_t = beats[i], float(duration)
            end_beat = n
        moments.append(Moment(
            idx=idx, start_t=start_t, end_t=end_t,
            start_beat=i, end_beat=end_beat, n_beats=end_beat - i,
        ))
        idx += 1
        if j > n - 1:
            break
        i += hop_beats

    return moments


def summarize(moments):
    """Quick human-readable stats for sanity-checking a segmentation."""
    if not moments:
        return "0 moments"
    lens = [m.end_t - m.start_t for m in moments]
    return (f"{len(moments)} moments | "
            f"len s: min {min(lens):.2f} / mean {sum(lens)/len(lens):.2f} / "
            f"max {max(lens):.2f}")
