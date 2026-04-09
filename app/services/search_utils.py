import re
from functools import lru_cache
from typing import List, Set, Iterable

# Maximum trigrams to keep per document/query to limit index size and work
TRIGRAM_MAX = 300


def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    # keep alnum and spaces
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@lru_cache(maxsize=2048)
def make_trigrams(s: str, max_trigrams: int = TRIGRAM_MAX) -> List[str]:
    """Return a deterministic list of character trigrams for the given string.

    The function normalizes text, computes per-token padded trigrams, and
    returns a sorted, bounded list to keep storage and index sizes reasonable.
    Uses an LRU cache to avoid recomputing for repeated queries/values.
    """
    s = _normalize_text(s)
    tris: Set[str] = set()
    for token in s.split():
        if not token:
            continue
        t = f"  {token}  "
        for i in range(len(t) - 2):
            tris.add(t[i : i + 3])
    tris_list = sorted(tris)
    if max_trigrams and len(tris_list) > int(max_trigrams):
        tris_list = tris_list[: int(max_trigrams)]
    return tris_list


def trigram_similarity(q_tris: Iterable[str], doc_tris: Iterable[str]) -> float:
    """Compute a similarity score between two trigram sets using Dice coefficient.

    Returns a float in [0.0, 1.0].
    """
    a = set(q_tris or [])
    b = set(doc_tris or [])
    if not a or not b:
        return 0.0
    inter = len(a & b)
    denom = len(a) + len(b)
    if denom == 0:
        return 0.0
    # Dice coefficient scaled between 0 and 1
    return (2.0 * inter) / denom