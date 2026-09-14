"""Meaning fingerprints (embeddings).

Turns a question into a list of numbers that represents its meaning.
Two questions that mean the same thing get similar numbers, even when
the words are completely different.

This is Stage 2 of the pipeline. Stage 1 (exact hashing) has already
removed the identical pairs, so this only runs on what is left.
"""

import numpy as np

# intfloat/multilingual-e5-base is built on XLM-RoBERTa, whose training
# corpus covers 100 languages including Sinhala and Tamil. It runs
# locally, so no question text ever leaves the machine, and re-indexing
# costs nothing.
DEFAULT_MODEL = "intfloat/multilingual-e5-base"


class Embedder:
    """Wraps the embedding model.

    Kept behind a small class so the model can be swapped later without
    touching any calling code.
    """

    def __init__(self, model_name=DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        print(f"Loading model: {model_name}")
        print("(first run downloads about 500 MB - this is a one-off)")
        self.model = SentenceTransformer(model_name)
        print("Model ready.\n")

    def _prefix(self, is_query):
        # E5 models REQUIRE this prefix. Leaving it out silently degrades
        # quality by a surprising amount - it is the single most common
        # mistake with this model family.
        if "e5" in self.model_name.lower():
            return "query: " if is_query else "passage: "
        return ""

    def encode(self, texts, is_query=False):
        """Return one vector per text, as a numpy array."""
        if isinstance(texts, str):
            texts = [texts]
        prefix = self._prefix(is_query)
        return self.model.encode(
            [prefix + t for t in texts],
            # Normalising here means cosine similarity becomes a plain
            # dot product later - same answer, less arithmetic.
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=len(texts) > 50,
        )


def cosine_similarity(a, b):
    """How close two meaning fingerprints are, from 0 to 1.

    Vectors are already normalised to length 1 when encoded, so the
    dot product IS the cosine similarity.
    """
    return float(np.dot(a, b))


def similarity_matrix(vectors):
    """Similarity of every vector against every other, in one operation.

    Far faster than looping. For n questions this returns an n x n grid
    where cell [i][j] is how similar question i is to question j.
    """
    v = np.asarray(vectors)
    return v @ v.T
