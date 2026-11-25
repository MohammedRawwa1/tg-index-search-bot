"""Word-form folding so search matches grammatical variants of a keyword.

Searching "create" must also find "created", "creating", "creates" and
"creation", and searching "creating" must find the plain "create" entries.
The same fold is applied to both the query token and the indexed token, so
the two sides always agree no matter which form the user typed.

This is not a linguistics-grade stemmer: it is a small, deterministic and
shared rule set (bot relevance engine, local indexer and the forward bot all
use it) so the same query behaves identically everywhere.

Rules, applied repeatedly (max two passes) to ASCII alphabetic tokens:

    plural / 3rd person        -s  -es  -ies
    past / participle          -ed -ied
    progressive                -ing (also -ings)
    comparative / adjective    -er -est -ly (-ingly, -edly)
    nominalisation             -ion -tion -sion -ment -ness -ance -ence

Plus consonant un-doubling (running -> run), a trailing "e" that the suffix
stripped (make/making -> mak) and a small derivational prefix list
(reinstall -> install, unlimited -> limit).

Tokens in other scripts (Arabic, Persian, Cyrillic, CJK, ...) are returned
unchanged: English inflection rules would only corrupt them.
"""

import re
from functools import lru_cache
from typing import Iterable, List, Set

# Derivational prefixes stripped when enough word is left behind.
_PREFIXES = (
    "under",
    "super",
    "inter",
    "multi",
    "over",
    "anti",
    "post",
    "non",
    "out",
    "mis",
    "dis",
    "pre",
    "sub",
    "un",
    "re",
)

# Inflectional / derivational suffixes and the form each folds to, longest
# first (the first match wins). Replacing instead of only trimming keeps
# "organization" -> "organiz" aligned with "organize"/"organizing" and
# "education"/"educational" -> "educat".
_REPLACEMENTS = (
    ("ational", "at"),
    ("ization", "iz"),
    ("isation", "is"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("ingly", ""),
    ("edly", ""),
    ("ments", ""),
    ("ment", ""),
    ("ness", ""),
    ("ings", ""),
    ("ing", ""),
    ("ers", ""),
    ("est", ""),
    ("ion", ""),
    ("er", ""),
    ("ed", ""),
    ("es", ""),
    ("ly", ""),
    ("s", ""),
)

_VOWELS = "aeiou"
# Letters whose doubling is usually part of the word (fall, class, buzz).
_NO_UNDOUBLE = "lsz"
# Words ending with these never lose a bare "s" (it is not a plural).
_NO_S_STRIP = ("ss", "us", "is", "os", "as")

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _fold_once(word: str) -> str:
    """Strip one suffix (or turn -ies/-ied into -i)."""
    if len(word) >= 5 and word.endswith(("ies", "ied")):
        return word[:-3] + "i"

    for suffix, replacement in _REPLACEMENTS:
        if not word.endswith(suffix):
            continue
        if len(word) - len(suffix) < 3:
            continue
        if suffix == "s" and word.endswith(_NO_S_STRIP):
            continue
        base = word[: -len(suffix)] + replacement
        # running -> runn -> run, stopped -> stopp -> stop
        if (
            len(base) >= 4
            and base[-1] == base[-2]
            and base[-1] not in _VOWELS
            and base[-1] not in _NO_UNDOUBLE
        ):
            base = base[:-1]
        return base
    return word


@lru_cache(maxsize=8192)
def fold(token: str) -> str:
    """Canonical form of a token, used on both query and indexed tokens."""
    word = (token or "").strip().lower()
    if len(word) < 3 or not word.isascii() or not word.isalpha():
        return word

    for _ in range(2):
        folded = _fold_once(word)
        if folded == word:
            break
        word = folded

    # "study" / "studies" -> "studi", "movie" / "movies" -> "movi"
    if len(word) >= 4 and word.endswith("y"):
        word = word[:-1] + "i"

    # "make" and "making" both end up as "mak".
    if len(word) >= 4 and word.endswith("e"):
        word = word[:-1]

    # "reinstall" / "install" -> "install".
    for prefix in _PREFIXES:
        if len(word) >= 7 and word.startswith(prefix) and len(word) - len(prefix) >= 5:
            word = word[len(prefix) :]
            break

    return word


def related(a: str, b: str) -> bool:
    """True when two tokens are the same word in different grammatical forms."""
    left = str(a or "").lower()
    right = str(b or "").lower()
    if not left or not right:
        return False
    return left == right or fold(left) == fold(right)


def matched_tokens(query_tokens: Iterable[str], doc_tokens: Iterable[str]) -> Set[str]:
    """Query tokens that have an exact or same-word-form counterpart in the doc."""
    queries = [str(t).lower() for t in query_tokens if t]
    docs = {str(t).lower() for t in doc_tokens if t}
    doc_forms = {fold(t) for t in docs}
    return {q for q in queries if q in docs or fold(q) in doc_forms}


_PREFIX_GROUP = f"(?:{'|'.join(sorted(_PREFIXES, key=len, reverse=True))})?"


def variant_patterns(tokens: Iterable[str]) -> List[str]:
    """Anchored regexes covering a token and its word-form variants.

    Used by the Mongo candidate queries: `^creat` finds "create", "created",
    "creating" and "creation", and the optional prefix group also finds
    "reinstall"/"unlimited"-style derivations. Candidates are still ranked by
    classify_match, so the extra recall never lowers precision by itself.
    """
    patterns: List[str] = []
    for token in tokens or []:
        token = str(token or "").strip().lower()
        if not token:
            continue
        stem = fold(token)
        if stem and stem != token:
            patterns.append("^" + _PREFIX_GROUP + re.escape(stem))
        patterns.append("^" + _PREFIX_GROUP + re.escape(token))

    seen = set()
    unique: List[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            unique.append(pattern)
    return unique
