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


def _variant_spellings(token: str) -> set[str]:
    """Return exact spellings that fold to the same canonical token."""
    stem = fold(token)
    roots = {stem}
    if stem and stem[-1] not in _VOWELS and stem[-1] not in _NO_UNDOUBLE:
        roots.add(stem + stem[-1])

    spellings = {token, stem}
    for root in roots:
        spellings.add(root + "e")
        if root.endswith("i"):
            spellings.update((root[:-1] + "y", root[:-1] + "ies", root[:-1] + "ied"))
        for suffix, replacement in _REPLACEMENTS:
            if replacement:
                if root.endswith(replacement):
                    spellings.add(root[: -len(replacement)] + suffix)
            else:
                spellings.add(root + suffix)
    # Keep only candidates that the canonicalizer confirms as equivalent.
    stem_spellings = {
        spelling for spelling in spellings if spelling and fold(spelling) == stem
    }
    for spelling in tuple(stem_spellings):
        for prefix in _PREFIXES:
            candidate = prefix + spelling
            if fold(candidate) == stem:
                stem_spellings.add(candidate)
    return stem_spellings


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


# Shortest stem the family rule will expand from. Below this, stemming would
# reduce a whole query to a three-letter fragment ("typing" -> "typ") and
# invite unrelated words, so those queries keep their literal spelling.
_MIN_FAMILY_ROOT = 4

# Derivational tails that split one word family into stems the inflection
# folder cannot reconcile: "produc-tion" vs "produce", "creat-ive" vs
# "creat-ion", "inform-ative" vs "inform-ation". Stripping them gives both
# sides the stem they share. The tail is only removed when at least five
# characters remain, so short roots keep their spelling.
_FAMILY_TAILS = ("iveness", "fulness", "ousness", "iv", "ic", "al", "ous", "ful")


def _family_extension(shorter: str, longer: str) -> bool:
    """True when `longer` opens with `shorter` (read -> reading, reader)."""
    return len(longer) > len(shorter) and longer.startswith(shorter)


def family_stem(token: str) -> str:
    """Derivational stem shared by members of one word family.

    `fold` reconciles inflections (create/creating/created) but not the
    derivational pairs English forms with a different tail
    (produce/production, create/creative, inform/information). This strips one
    such tail so both sides compare equal, for any word rather than a list of
    exception words.
    """
    raw = str(token or "").strip().lower()
    word = fold(raw)
    # -tion words are built as base + tion (produc + tion) while their verb is
    # spelled base + e (produce). Dropping the whole tail gives the shared base.
    # "action"/"fiction" are base + ion, so a base shorter than four letters
    # keeps the folded form instead of shrinking to "ac"/"fic".
    if len(raw) >= 8 and raw.endswith(("tion", "sion")):
        base = raw[:-4]
        if len(base) >= 4:
            return fold(base)
    if len(word) < 6:
        return word
    for tail in _FAMILY_TAILS:
        if word.endswith(tail) and len(word) - len(tail) >= 5:
            word = word[: -len(tail)]
            break
    # The -ct- / -c- alternation: productive/production vs produce,
    # attractive vs attraction. Dropping the t gives every member of that
    # family the same base.
    if word.endswith("ct") and len(word) >= 6:
        return word[:-1]
    return word


def family_root(token: str) -> str:
    """Spelling the family expansion starts from for `token`.

    The literal token is used unless folding strips an inflection and leaves
    a stem long enough to be a word root: "reading" -> "read" (so searching
    any member of that family reaches the whole family, not only longer
    spellings of "reading"), while "read" and "reader" stay themselves and
    "typing" keeps its spelling because its stem "typ" is too short to expand
    safely.
    """
    word = str(token or "").strip().lower()
    if len(word) < _MIN_FAMILY_ROOT:
        return word
    stem = fold(word)
    if stem and stem != word and len(stem) >= _MIN_FAMILY_ROOT:
        return stem
    return word


def extends_query(query_token: str, doc_token: str) -> bool:
    """True when `doc_token` belongs to the word family of `query_token`.

    Word-agnostic: every typed token is expanded from its own frames, never
    from a list of known words. The whole family that opens with the query's
    root is invited in (read -> reading, reader; type -> typing, typist), and
    a longer member starts from its stem so "reading" reaches "read" and
    "readme" instead of only longer "reading" spellings.

    The second rule joins the derivational stems that the inflection folder
    cannot reconcile: "production" reaches "produce"/"producing" (shared stem
    "produc"), "creative" reaches "create"/"creation" (shared stem "creat"),
    "informative" reaches "information". That comparison only applies once the
    shared stem is at least five characters long, so it never collapses a long
    query onto a short root: "reading" still does not match every "read" file.

    Case never matters because both sides are lowercased here. Grammatical
    variants that fold to the same spelling are handled by :func:`fold` and
    are not repeated here.
    """
    query = str(query_token or "").strip().lower()
    doc = str(doc_token or "").strip().lower()
    if len(query) < 3 or len(doc) < 3:
        return False
    if query == doc or fold(query) == fold(doc):
        return False
    if _family_extension(family_root(query), doc):
        return True
    query_stem = family_stem(query)
    doc_stem = family_stem(doc)
    if not query_stem or not doc_stem:
        return False
    if query_stem == doc_stem:
        return True
    shorter, longer = (
        (query_stem, doc_stem)
        if len(query_stem) <= len(doc_stem)
        else (doc_stem, query_stem)
    )
    return len(shorter) >= 5 and longer.startswith(shorter)


def family_prefix_terms(tokens: Iterable[str]) -> List[str]:
    """Unique spellings to expand a family from: each typed token, its root
    and its derivational stem.

    Tokens are lowercased, de-duplicated and kept in order. For "reading"
    this yields ["reading", "read"]; for "production" it yields
    ["production", "product", "produc"], so the typed form, its root and the
    wider stem family are all retrieved - for any word.
    """
    terms: List[str] = []
    seen = set()
    for token in tokens or []:
        word = str(token or "").strip().lower()
        if not word:
            continue
        for form in (word, family_root(word), family_stem(word)):
            if form and form not in seen:
                seen.add(form)
                terms.append(form)
    return terms


def prefix_patterns(tokens: Iterable[str], boundary: bool = False) -> List[str]:
    """Start-anchored regexes retrieving every word-family extension of `tokens`.

    For "read" this yields `^read`, which retrieves the whole family - "read",
    "reads", "reading", "reader", "readme", ... and, through the boundary
    form, "my_reading_list" inside a filename. A longer member query also
    emits its stem ("reading" -> `^read`) so the rest of its family is
    retrieved, but never the shorter root itself: "reading" must not retrieve
    every "read" file, so no reversed or reconstructed-root pattern is added.
    Patterns stay anchored at the start so the `title_tokens` index remains
    usable. With `boundary=True` the patterns match inside a filename
    ("Reading.pdf") instead of a whole token array element.
    """
    lead = r"(?:^|[^0-9a-z])" if boundary else "^"
    patterns: List[str] = []
    for token in family_prefix_terms(tokens):
        if len(token) < 3:
            continue
        patterns.append(lead + re.escape(token))

    seen = set()
    unique: List[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            unique.append(pattern)
    return unique


def variant_patterns(tokens: Iterable[str]) -> List[str]:
    """Anchored regexes for exact tokens and recognized word-form variants."""
    patterns: List[str] = []
    for token in tokens or []:
        token = str(token or "").strip().lower()
        if not token:
            continue
        spellings = "|".join(
            re.escape(spelling) for spelling in sorted(_variant_spellings(token))
        )
        patterns.append("^(?:" + spellings + ")$")

    seen = set()
    unique: List[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            unique.append(pattern)
    return unique
