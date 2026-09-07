"""Tests for the similarity maths.

These use hand-made vectors rather than the real model, so they run
instantly and prove the pipeline logic independently of whether the
model is downloaded.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.embeddings import cosine_similarity, similarity_matrix


def unit(v):
    """Normalise to length 1, the way the encoder does."""
    v = np.array(v, dtype=float)
    return v / np.linalg.norm(v)


def test_identical_vectors_score_one():
    v = unit([1, 2, 3])
    assert cosine_similarity(v, v) == 1.0


def test_opposite_vectors_score_minus_one():
    a = unit([1, 0, 0])
    b = unit([-1, 0, 0])
    assert round(cosine_similarity(a, b), 6) == -1.0


def test_perpendicular_vectors_score_zero():
    a = unit([1, 0])
    b = unit([0, 1])
    assert round(cosine_similarity(a, b), 6) == 0.0


def test_similar_vectors_score_high():
    a = unit([1.0, 0.0, 0.0])
    b = unit([0.95, 0.05, 0.0])
    assert cosine_similarity(a, b) > 0.9


def test_matrix_diagonal_is_all_ones():
    vectors = np.array([unit([1, 0, 0]), unit([0, 1, 0]), unit([1, 1, 0])])
    sim = similarity_matrix(vectors)
    for i in range(3):
        assert round(float(sim[i][i]), 6) == 1.0


def test_matrix_is_symmetric():
    vectors = np.array([unit([1, 2, 3]), unit([3, 2, 1]), unit([1, 1, 1])])
    sim = similarity_matrix(vectors)
    for i in range(3):
        for j in range(3):
            assert round(float(sim[i][j]), 9) == round(float(sim[j][i]), 9)


def test_matrix_matches_pairwise():
    """The fast matrix path must agree with the simple per-pair path."""
    vectors = np.array([unit([1, 2, 3]), unit([3, 2, 1]), unit([0, 1, 4])])
    sim = similarity_matrix(vectors)
    for i in range(3):
        for j in range(3):
            expected = cosine_similarity(vectors[i], vectors[j])
            assert round(float(sim[i][j]), 9) == round(expected, 9)
