"""MomentIndex — the similarity engine's core contracts.

Uses tiny synthetic *_moments.npz files (the index is schema-agnostic about
facet width, so 8-dim embeddings stand in for the real 768-dim MERT ones).
"""

import os

import numpy as np
import pytest

from moment_index import MomentIndex


def _write_song(tmp_path, sid, embs):
    """embs: (n, d) — one L2-normalizable row per moment."""
    embs = np.asarray(embs, np.float32)
    n = embs.shape[0]
    np.savez(
        os.path.join(tmp_path, f"{sid}_moments.npz"),
        start_t=np.arange(n, dtype=np.float32) * 5.0,
        end_t=np.arange(1, n + 1, dtype=np.float32) * 5.0,
        interactions=np.random.default_rng(0).normal(size=(n, 4)).astype(np.float32),
        descriptors=np.random.default_rng(1).normal(size=(n, 3)).astype(np.float32),
        emb_mix=embs,
    )


@pytest.fixture()
def index(tmp_path):
    # song A: 3 moments; the 3rd points the same way as B's 1st (cosine ~1)
    _write_song(tmp_path, "A", [[1, 0, 0, 0, 0, 0, 0, 0],
                                [0, 1, 0, 0, 0, 0, 0, 0],
                                [0, 0, 1, 0, 0, 0, 0, 0]])
    _write_song(tmp_path, "B", [[0, 0, 2, 0, 0, 0, 0, 0],     # parallel to A#2
                                [0, 0, 0, 1, 0, 0, 0, 0]])
    idx = MomentIndex.from_dir(str(tmp_path))
    return idx


def test_rows_match_moment_counts(index):
    assert len(index.rows) == 5
    assert index.facets["emb_mix"].shape == (5, 8)


def test_rows_and_facets_stay_aligned(index):
    # row r of every facet matrix must describe rows[r]
    a2 = next(r for r, (sid, mi, _, _) in enumerate(index.rows)
              if sid == "A" and mi == 2)
    v = index.facets["emb_mix"][a2]
    assert np.argmax(np.abs(v)) == 2          # A's 3rd moment points along dim 2


def test_query_finds_the_parallel_moment_first(index):
    res = index.query("A", 2, weights={"emb_mix": 1.0}, k=3)
    assert res[0]["song_id"] == "B" and res[0]["moment_idx"] == 0
    assert res[0]["score"] == pytest.approx(1.0, abs=1e-5)
    # scores sorted descending
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)


def test_exclude_same_song(index):
    res = index.query("A", 0, weights={"emb_mix": 1.0}, k=10)
    assert all(r["song_id"] != "A" for r in res)
    res_incl = index.query("A", 0, weights={"emb_mix": 1.0}, k=10,
                           exclude_same_song=False)
    assert any(r["song_id"] == "A" for r in res_incl)


def test_unknown_seed_raises(index):
    with pytest.raises(KeyError):
        index.query("A", 99)


def test_missing_facet_key_does_not_misalign_rows(tmp_path):
    # song M lacks emb_vocals entirely (e.g. no vocals stem when indexed);
    # later songs' rows must NOT shift in that facet matrix
    embs_m = np.eye(2, 8, dtype=np.float32)
    np.savez(os.path.join(tmp_path, "M_moments.npz"),
             start_t=np.array([0.0, 5.0], np.float32),
             end_t=np.array([5.0, 10.0], np.float32),
             interactions=np.zeros((2, 4), np.float32),
             descriptors=np.zeros((2, 3), np.float32),
             emb_mix=embs_m)                          # note: NO emb_vocals
    v = np.zeros((2, 8), np.float32); v[0, 3] = 1; v[1, 4] = 1
    np.savez(os.path.join(tmp_path, "N_moments.npz"),
             start_t=np.array([0.0, 5.0], np.float32),
             end_t=np.array([5.0, 10.0], np.float32),
             interactions=np.zeros((2, 4), np.float32),
             descriptors=np.zeros((2, 3), np.float32),
             emb_mix=np.eye(2, 8, dtype=np.float32),
             emb_vocals=v)
    idx = MomentIndex.from_dir(str(tmp_path))
    assert idx.facets["emb_vocals"].shape[0] == len(idx.rows) == 4
    n0 = next(r for r, (sid, mi, _, _) in enumerate(idx.rows)
              if sid == "N" and mi == 0)
    assert np.argmax(np.abs(idx.facets["emb_vocals"][n0])) == 3   # N's rows, not shifted
    m_rows = [r for r, (sid, *_id) in enumerate(idx.rows) if sid == "M"]
    assert np.allclose(idx.facets["emb_vocals"][m_rows], 0)       # zero-filled, never match


def test_zero_vector_embedding_does_not_poison_scores(tmp_path):
    # an all-zero moment embedding must not create NaNs that outrank real scores
    _write_song(tmp_path, "Z", [[0, 0, 0, 0, 0, 0, 0, 0],
                                [1, 0, 0, 0, 0, 0, 0, 0]])
    _write_song(tmp_path, "W", [[1, 0, 0, 0, 0, 0, 0, 0]])
    idx = MomentIndex.from_dir(str(tmp_path))
    res = idx.query("W", 0, weights={"emb_mix": 1.0}, k=2)
    assert all(np.isfinite(r["score"]) for r in res)
    assert res[0]["song_id"] == "Z" and res[0]["moment_idx"] == 1
