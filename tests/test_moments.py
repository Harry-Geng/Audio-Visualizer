"""segment_by_beats — the moment grid every downstream index hangs off."""

from moments import segment_by_beats


def uniform_beats(n, step=0.5):
    return [i * step for i in range(n)]


def test_no_beats_falls_back_to_whole_song():
    ms = segment_by_beats([], duration=200.0)
    assert len(ms) == 1
    assert ms[0].start_t == 0.0 and ms[0].end_t == 200.0


def test_single_beat_falls_back_to_whole_song():
    ms = segment_by_beats([12.0], duration=200.0)
    assert len(ms) == 1
    assert ms[0].end_t == 200.0


def test_windows_are_beat_aligned_with_50pct_overlap():
    beats = uniform_beats(33)                    # 0.0 .. 16.0 s
    ms = segment_by_beats(beats, duration=16.5)
    # full windows start every hop (4 beats) and span 8 beats (4.0 s)
    full = [m for m in ms if m.n_beats == 8]
    assert full, "expected full windows"
    for m in full:
        assert abs((m.end_t - m.start_t) - 4.0) < 1e-9
        assert abs(m.start_t - m.start_beat * 0.5) < 1e-9
    # hops: consecutive full windows start 4 beats apart
    starts = [m.start_beat for m in full]
    assert all(b - a == 4 for a, b in zip(starts, starts[1:]))


def test_indices_are_sequential_and_times_ordered():
    ms = segment_by_beats(uniform_beats(40), duration=20.0)
    assert [m.idx for m in ms] == list(range(len(ms)))
    for m in ms:
        assert m.end_t > m.start_t


def test_short_tail_is_dropped():
    # 10 beats: after the first full window (i=0..8) the remainder from i=4
    # is 5 beats (>= min_tail default 4) -> tail kept; with 9 beats the
    # remainder is 4 -> still kept; with min_tail_beats raised it's dropped.
    ms_keep = segment_by_beats(uniform_beats(10), duration=5.0)
    assert ms_keep[-1].end_t == 5.0
    ms_drop = segment_by_beats(uniform_beats(10), duration=5.0, min_tail_beats=6)
    assert all(m.n_beats == 8 for m in ms_drop)


def test_first_window_short_song_still_emits_one_moment():
    # fewer beats than one window -> a single tail moment spanning to duration
    ms = segment_by_beats(uniform_beats(5), duration=3.3)
    assert len(ms) == 1
    assert ms[0].start_t == 0.0 and ms[0].end_t == 3.3
