"""Deterministic local embeddings.

Why not a hosted embedding model: this project has to run with no API key and
produce byte-identical eval results on two different machines. A hashed
bag-of-words projection with sublinear term weighting is enough to retrieve the
right policy section out of a small, curated corpus, and it is reproducible.

Retrieval quality is measured in the eval suite (``policy_retrieval_recall``),
so swapping this for a hosted embedding model is a one-function change with a
number attached to it.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from app.config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can did do does for from had has have how i if in into is
    it its me my no not of on or our so than that the their them then there these they this to
    too was we were what when where which who why will with would you your
    """.split()
)

# Support vocabulary is small and domain-specific; a few hand-tuned synonym
# groups do more for recall here than a bigger model would.
_SYNONYMS = {
    "money": "refund",
    "refunded": "refund",
    "refunds": "refund",
    "reimburse": "refund",
    "reimbursement": "refund",
    "returned": "return",
    "returns": "return",
    "sending": "return",
    "sent": "return",
    "broken": "damaged",
    "cracked": "damaged",
    "defective": "damaged",
    "faulty": "damaged",
    "smashed": "damaged",
    "dented": "damaged",
    "parcel": "shipment",
    "package": "shipment",
    "delivery": "shipment",
    "courier": "shipment",
    "shipping": "shipment",
    "tracking": "shipment",
    "late": "delay",
    "delayed": "delay",
    "slow": "delay",
    "stuck": "delay",
    "checked": "inspection",
    "inspected": "inspection",
    "qc": "inspection",
    "window": "window",
    "deadline": "window",
    "headphone": "headphones",
    "earphones": "headphones",
    "earbuds": "headphones",
}


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOPWORDS or len(raw) < 2:
            continue
        tokens.append(_SYNONYMS.get(raw, raw))
    return tokens


def _bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


def embed(text: str, dim: int | None = None) -> list[float]:
    dim = dim or get_settings().embedding_dim
    vector = [0.0] * dim
    counts = Counter(tokenize(text))
    if not counts:
        return vector
    for token, count in counts.items():
        index, sign = _bucket(token, dim)
        vector[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))


def lexical_overlap(query: str, document: str) -> float:
    """Weighted Jaccard on the token sets -- cheap, and it rescues cases where
    hashing collisions blur the vector signal."""
    q = Counter(tokenize(query))
    d = Counter(tokenize(document))
    if not q or not d:
        return 0.0
    shared = sum(min(count, d[token]) for token, count in q.items())
    return shared / max(1, sum(q.values()))


def hybrid_score(query: str, query_vec: list[float], chunk_vec: list[float], chunk_text: str) -> float:
    """0.65 dense + 0.35 lexical. Tuned on the dev split of the eval set."""
    return 0.65 * max(0.0, cosine(query_vec, chunk_vec)) + 0.35 * lexical_overlap(
        query, chunk_text
    )
