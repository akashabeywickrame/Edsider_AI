"""Tests for the one-against-many check.

Hand-made vectors again, so these run instantly and test the pipeline
logic rather than the model. A vector here is just a direction: two
questions pointing the same way are "similar" whatever their words.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dedupe


def unit(v):
    v = np.array(v, dtype=float)
    return v / np.linalg.norm(v)


def q(qid, text, vector, **meta):
    row = {"id": qid, "text": text, "vector": list(unit(vector))}
    row.update(meta)
    return row


# A bank where Q001 and Q002 point almost the same way, and Q003 does not.
BANK = [
    q("Q001", "Calculate the area of a rectangle 12 cm by 5 cm",
      [1.0, 0.0, 0.0], grade="8", subject="Mathematics"),
    q("Q002", "Find the area of a rectangle measuring 12 cm and 5 cm",
      [0.99, 0.1, 0.0], grade="8", subject="Mathematics"),
    q("Q003", "Name the largest planet in the solar system",
      [0.0, 0.0, 1.0], grade="8", subject="Science"),
]


def test_finds_the_near_match():
    new = q("QNEW", "Work out the area of a rectangle 12 cm by 5 cm",
            [1.0, 0.02, 0.0], grade="8", subject="Mathematics")
    hits = dedupe.check_one(new, BANK, threshold=0.85)
    assert hits
    assert hits[0]["match"]["id"] in ("Q001", "Q002")
    assert hits[0]["verdict"] == "LIKELY DUPLICATE"


def test_unrelated_question_matches_nothing():
    new = q("QNEW", "Name the largest ocean", [0.0, 1.0, 0.0])
    assert dedupe.check_one(new, BANK, threshold=0.85) == []


def test_empty_bank_is_not_an_error():
    new = q("QNEW", "anything", [1.0, 0.0, 0.0])
    assert dedupe.check_one(new, [], threshold=0.85) == []


def test_a_question_does_not_match_itself():
    """Re-checking something already in the bank must not report it as
    its own duplicate."""
    same = dict(BANK[0])
    hits = dedupe.check_one(same, BANK, threshold=0.85)
    assert all(h["match"]["id"] != "Q001" for h in hits)


def test_guards_demote_a_look_alike():
    """The pair that motivates the whole guard layer: same numbers,
    same shape, different thing being asked."""
    new = q("QNEW", "Calculate the perimeter of a rectangle 12 cm by 5 cm",
            [1.0, 0.0, 0.0], grade="8", subject="Mathematics")

    without = dedupe.check_one(new, BANK, use_guards=False, threshold=0.85)
    with_guards = dedupe.check_one(new, BANK, use_guards=True, threshold=0.85)

    top_off = next(h for h in without if h["match"]["id"] == "Q001")
    top_on = next(h for h in with_guards if h["match"]["id"] == "Q001")

    assert top_off["verdict"] == "LIKELY DUPLICATE"
    assert top_on["score"] < top_off["score"]
    assert top_on["verdict"] == "DISTINCT"
    assert any("area" in r and "perimeter" in r for r in top_on["reasons"])


def test_demoted_matches_are_still_returned_with_a_reason():
    """A reviewer needs to see what was ruled out and why, not silence."""
    new = q("QNEW", "Calculate the perimeter of a rectangle 12 cm by 5 cm",
            [1.0, 0.0, 0.0], grade="8", subject="Mathematics")
    hits = dedupe.check_one(new, BANK, threshold=0.85)
    assert hits, "the near match should still be reported"
    assert hits[0]["reasons"]


def test_guards_never_raise_a_score():
    new = q("QNEW", "Find the area of a rectangle 12 cm by 5 cm",
            [1.0, 0.01, 0.0], grade="8", subject="Mathematics")
    for h in dedupe.check_one(new, BANK, threshold=0.80):
        assert h["score"] <= h["raw_score"]
        assert h["penalty"] >= 0.0


def test_top_k_is_respected():
    new = q("QNEW", "area of a rectangle", [1.0, 0.05, 0.0])
    assert len(dedupe.check_one(new, BANK, threshold=0.5, top_k=1)) == 1


def test_look_alikes_cannot_crowd_out_the_real_match():
    """Truncating on the RAW score let a cluster of near-copies fill every
    slot and then collapse under their penalties, hiding the one genuine
    duplicate - which scores lower raw precisely because it is a rewording.
    Guards must run before truncation, not after."""
    bank = [
        q(f"AREA{i}", f"Calculate the area of a rectangle {12 + i} cm by 5 cm",
          [1.0, 0.01 * i, 0.0], grade="8", subject="Mathematics")
        for i in range(6)
    ]
    bank.append(
        q("REAL", "Work out the perimeter of a rectangle 12 cm by 5 cm",
          [1.0, 0.25, 0.0], grade="8", subject="Mathematics")
    )
    new = q("QNEW", "Find the perimeter of a rectangle 12 cm by 5 cm",
            [1.0, 0.0, 0.0], grade="8", subject="Mathematics")

    hits = dedupe.check_one(new, bank, threshold=0.85, top_k=5)

    assert hits[0]["match"]["id"] == "REAL", \
        f"real duplicate lost; got {[h['match']['id'] for h in hits]}"
    assert hits[0]["score"] > 0.9


def test_accepts_a_json_string_vector():
    """Vectors come out of SQLite as JSON text, not lists."""
    bank = [dict(r, vector="[1.0, 0.0, 0.0]") for r in BANK[:1]]
    new = dict(q("QNEW", "area of a rectangle 12 cm by 5 cm",
                 [1.0, 0.0, 0.0], grade="8", subject="Mathematics"))
    new["vector"] = "[1.0, 0.0, 0.0]"
    hits = dedupe.check_one(new, bank, threshold=0.85)
    assert hits and hits[0]["match"]["id"] == "Q001"


def test_vector_index_gives_the_same_answers_as_plain_rows():
    """The index exists to avoid re-parsing JSON vectors on every call.
    It must not change a single result."""
    new = q("QNEW", "Find the area of a rectangle 12 cm by 5 cm",
            [1.0, 0.01, 0.0], grade="8", subject="Mathematics")
    plain = dedupe.check_one(new, BANK, threshold=0.80)
    indexed = dedupe.check_one(new, dedupe.VectorIndex(BANK), threshold=0.80)

    assert [h["match"]["id"] for h in plain] == \
           [h["match"]["id"] for h in indexed]
    for a, b in zip(plain, indexed):
        assert round(a["score"], 5) == round(b["score"], 5)


def test_find_pairs_matches_across_the_block_boundary():
    """The sweep compares a block of questions at a time. A pair split
    across two blocks must still be found."""
    n = dedupe.BLOCK + 40
    # Random directions, so the filler is mutually dissimilar and the
    # sweep does not spend its time guard-checking noise.
    rng = np.random.default_rng(7)
    bank = [q(f"Q{i}", f"filler question number {i}", rng.normal(size=16))
            for i in range(n)]
    # One identical pair, deliberately either side of the boundary.
    same = rng.normal(size=16)
    bank[3] = q("EARLY", "Explain photosynthesis in green plants", same)
    bank[-2] = q("LATE", "Explain photosynthesis in green plants", same)

    pairs = dedupe.find_pairs(bank, threshold=0.99)
    found = {tuple(sorted((p["a"], p["b"]))) for p in pairs}
    assert ("EARLY", "LATE") in found


def test_metadata_guard_fires_across_subjects():
    # Worded differently from Q001 on purpose. Identical text across a
    # grade or subject boundary is one question filed twice, and the
    # guard deliberately stays silent on that - see test_guards.py.
    new = q("QNEW", "Calculate the area of a rectangle measuring 12 by 5 cm",
            [1.0, 0.0, 0.0], grade="10", subject="Science")
    hit = next(h for h in dedupe.check_one(new, BANK, threshold=0.5)
               if h["match"]["id"] == "Q001")
    assert any("grade or subject" in r for r in hit["reasons"])
