"""Tiered, precision-first search relevance engine (shared by bot + web API).

Implements the tiered search pipeline recommended for the file index:

    Tier 0  exact   normalized filename/title equals the query          +100
    Tier 1  phrase  all query tokens as a contiguous phrase             +90
    Tier 2  all     every query token present in title tokens           +80
    Tier 3  most    >= MOST_TOKENS_RATIO of query tokens present        +60
    Tier 4  prefix  full multi-token query prefixes a filename          +40
    Tier 5  typo    trigram gate plus one-edit token match               +20
    Tier 6  broad   conservative last resort (regex) — opt-in only       +5

A higher tier ALWAYS outranks a lower tier (precision first, recall last).
Within a tier, fine-grained signals (token count, quality/codec/year,
filename, recency) act as tie-breakers but can never cross tier boundaries.

The broad (regex) tier is deliberately NOT part of the normal search path;
it is only reachable when an explicit `allow_broad=True` request is made.

All thresholds/weights are tunable via environment variables so the bots can
be tuned remotely without code changes:

    SEARCH_MIN_TIER        starting minimum accepted tier (default "most")
    SEARCH_MIN_RESULTS     broaden to the next tier below this many hits (default 5)
    MOST_TOKENS_RATIO       fraction of query tokens required for "most" (default 0.6)
    TRIGRAM_MIN_SIM         minimum Dice coefficient for the typo tier (default 0.22)
    SEARCH_QUALITY_LOG      set "0"/"false" to disable per-query quality logging
"""

import math
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.morphology import (
    extends_query as _extends_query,
    fold as _fold,
    matched_tokens as _matched_tokens,
)
from app.services.search_utils import make_trigrams, trigram_similarity, TRIGRAM_MAX
from app.services.textnorm import normalize_text as _normalize_text
from app.services.tokenizer import tokenize_filename
from app.utils.logger import logger

# ---------------------------------------------------------------------------
# Tier table (priority doubles as the dominant score component)
# ---------------------------------------------------------------------------

# Order: highest precision first. Used to pick the strongest tier a doc
# qualifies for and to drive auto-broadening.
TIER_ORDER = ["exact", "phrase", "all", "most", "prefix", "typo", "broad"]

TIER_PRIORITY: Dict[str, float] = {
    "exact": 100.0,
    "phrase": 90.0,
    "all": 80.0,
    "most": 60.0,
    "prefix": 40.0,
    "typo": 20.0,
    "broad": 5.0,
}

# Sentinel tier for docs that qualify for nothing in the normal path.
NONE_TIER = "none"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


# Tiers that are acceptable without any broadening (precision default).
DEFAULT_MIN_TIER = os.getenv("SEARCH_MIN_TIER", "most").strip().lower()
if DEFAULT_MIN_TIER not in TIER_PRIORITY:
    DEFAULT_MIN_TIER = "most"

# Broaden to the next tier when a tier yields fewer than this many results.
MIN_RESULTS = _env_int("SEARCH_MIN_RESULTS", 5)

# Fraction of query tokens that must match for the "most" tier
# (at least half, so "breaking bad season 3" docs matching only
# "breaking bad" still rank as most-token matches).
MOST_TOKENS_RATIO = _env_float("MOST_TOKENS_RATIO", 0.5)

# Minimum Dice coefficient for a doc to qualify for the "typo" tier.
TRIGRAM_MIN_SIM = _env_float("TRIGRAM_MIN_SIM", 0.22)

# Cap on fine-grained tie-breaker bonuses so they can never cross tiers
# (the smallest tier gap is 10 between "phrase" and "all").
FINE_SCORE_CAP = 9.0

QUALITY_LOGGING = os.getenv("SEARCH_QUALITY_LOG", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_text(text: Optional[str]) -> str:
    """Case-folded, word-only normalized form (single spaces).

    Delegates to :mod:`app.services.textnorm` so words in any script (Arabic,
    Persian, Cyrillic, CJK, ...) survive instead of being deleted by a
    Latin-only pattern. Re-exported here for existing callers.
    """
    return _normalize_text(text)


def tokenize_norm(text: Optional[str]) -> List[str]:
    """Normalized whitespace tokens of `text`."""
    norm = normalize_text(text)
    return norm.split() if norm else []


def is_contiguous_phrase(q_tokens: Sequence[str], doc_tokens: Sequence[str]) -> bool:
    """True when `q_tokens` appear consecutively (in order) inside `doc_tokens`.

    Compared with word-form folding, so "playing games" matches the title
    tokens "play game" and the other way round.
    """
    if len(q_tokens) < 2 or len(q_tokens) > len(doc_tokens):
        return False
    q_forms = [_fold(t) for t in q_tokens]
    doc_forms = [_fold(t) for t in doc_tokens]
    q_len = len(q_forms)
    for i in range(len(doc_forms) - q_len + 1):
        if doc_forms[i : i + q_len] == q_forms:
            return True
    return False


def _one_edit_apart(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return False

    i = j = edits = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(left) > len(right):
            i += 1
        elif len(right) > len(left):
            j += 1
        else:
            i += 1
            j += 1
    edits += (len(left) - i) + (len(right) - j)
    return edits == 1


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class MatchInfo:
    __slots__ = ("tier", "priority", "coverage", "matched", "flags")

    def __init__(
        self,
        tier: str,
        coverage: float = 0.0,
        matched: int = 0,
        flags: Optional[Dict[str, Any]] = None,
    ):
        self.tier = tier
        self.priority = TIER_PRIORITY.get(tier, 0.0)
        self.coverage = float(coverage)
        self.matched = int(matched)
        self.flags = flags or {}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"MatchInfo(tier={self.tier!r}, priority={self.priority}, coverage={self.coverage:.2f}, matched={self.matched})"


def tier_priority(tier: Optional[str]) -> float:
    return TIER_PRIORITY.get(tier or "", 0.0)


def _doc_title_tokens(doc: Dict[str, Any]) -> List[str]:
    filename = doc.get("filename")
    if isinstance(filename, str) and filename:
        tokens = tokenize_filename(filename).get("title_tokens") or []
        return [str(t).lower() for t in tokens if t]

    tt = doc.get("title_tokens") or []
    return [str(t).lower() for t in tt if t]


def classify_match(
    query: str,
    tokens: Sequence[str],
    doc: Dict[str, Any],
    q_tris: Optional[Sequence[str]] = None,
    allow_broad: bool = False,
) -> MatchInfo:
    """Return the strongest tier this document qualifies for.

    `tokens` must be the tokenizer output for `query` (already lowercased).
    `q_tris` is the query's trigram set used for the "typo" tier; it is
    computed lazily when omitted. Returns MatchInfo("none", ...) when the
    doc does not qualify for any tier in the normal path.
    """
    q_tokens = [t.lower() for t in tokens if t]
    if not q_tokens:
        return MatchInfo("broad" if allow_broad else "none", 0.0, 0)

    doc_titles = _doc_title_tokens(doc)
    title_set = set(doc_titles)
    q_set = set(q_tokens)
    # year tokens act as filters, not title tokens — drop them from coverage
    q_year = None
    for t in list(q_set):
        if re.fullmatch(r"(19|20)\d{2}", t):
            q_year = t
            q_set.discard(t)
    try:
        doc_year = doc.get("year")
        doc_year_s = str(int(doc_year)) if doc_year is not None else None
    except Exception:
        doc_year_s = None

    # year-only query (e.g. "2010"): match against the indexed year directly
    if not q_set and q_year:
        if doc_year_s == q_year:
            return MatchInfo("all", 1.0, 1, {"year": True})
        return MatchInfo("broad" if allow_broad else NONE_TIER, 0.0, 0)

    # quality/codec tokens are legitimate matches too (e.g. "1080p")
    aux_set = set(str(t).lower() for t in (doc.get("quality_tokens") or []))
    aux_set |= set(str(t).lower() for t in (doc.get("codec_tokens") or []))

    # exact tokens first, then the same word in another grammatical form, so
    # "create" also matches "created", "creating", "creates" and "creation"
    doc_token_set = title_set | aux_set
    exact_matched = _matched_tokens(q_set, doc_token_set)

    # word-family prefixes: the whole family that opens with the query's root
    # is invited in, so "read" reaches "reading"/"reader"/"readme" (and
    # "my_reading_list", whose tokenizer split makes "reading" a token of its
    # own). Searching a longer member expands from its stem too ("reading" ->
    # "read" family), so it finds "readme" while still never collapsing onto
    # every "read" entry.
    # Only tokens with no exact form are considered. Family matches count as
    # matches, so the whole merged family sits in the same tier as the literal
    # hits instead of being dropped or pushed past the last page of results.
    family_matched = {
        q
        for q in q_set - exact_matched
        if any(_extends_query(q, d) for d in doc_token_set)
    }
    matched = len(exact_matched) + len(family_matched)
    coverage = matched / float(len(q_set)) if q_set else 0.0
    # remembered for the caller: word-family hits can be lifted into the first
    # page even though a big library orders the literal hits ahead of them.
    try:
        doc["_family"] = len(family_matched)
    except Exception:
        pass

    # --- exact: normalized filename / title equals (or is a full prefix of)
    #     the normalized query. The prefix form handles indexed filenames that
    #     carry trailing release junk (extension/quality tokens).
    q_norm = normalize_text(query or " ".join(q_tokens))
    fname = doc.get("filename") or ""
    fname_norm = normalize_text(fname)
    title_str = " ".join(doc_titles).strip()
    try:
        doc_norm = doc.get("norm_filename") or ""
        if not doc_norm:
            doc_norm = title_str
        doc_norm = str(doc_norm).strip().lower()
    except Exception:
        doc_norm = title_str
    if q_norm:
        if (
            q_norm == fname_norm
            or q_norm == doc_norm
            or (title_str and q_norm == title_str)
        ):
            return MatchInfo("exact", 1.0, matched, {"filename": fname_norm == q_norm})
        # full-title prefix: the normalized name starts with the full query and
        # only a short run of release-junk tokens follows (e.g. "...mkv").
        # A partial prefix like "harry pot" does NOT qualify.
        if len(q_tokens) >= 2:
            for cand in (doc_norm, fname_norm):
                if cand.startswith(q_norm + " "):
                    remainder = cand[len(q_norm) :].strip().split()
                    if 0 < len(remainder) <= 2:
                        return MatchInfo(
                            "exact", 1.0, matched, {"filename_prefix": True}
                        )

    # --- phrase: all tokens as a contiguous sequence in the title ---
    if len(q_tokens) >= 2 and is_contiguous_phrase(q_tokens, doc_titles):
        return MatchInfo("phrase", coverage, matched)

    # --- all tokens present ---
    if matched == len(q_set):
        return MatchInfo("all", 1.0, matched)

    # --- most tokens present ---
    # A multi-word query must match at least two words. With two tokens a
    # half-ratio "most" accepted a single-token hit, so "read speed" returned
    # every book containing "read".
    min_most = 1
    if len(q_set) >= 2:
        min_most = max(2, math.ceil(len(q_set) * MOST_TOKENS_RATIO))
    if matched >= min_most and coverage >= MOST_TOKENS_RATIO:
        return MatchInfo("most", coverage, matched)

    # A full multi-token filename prefix is useful; individual token prefixes
    # are not word-form matches ("read" must not normalize to "reading").
    if len(q_tokens) > 1 and q_norm and fname_norm.startswith(q_norm):
        return MatchInfo("prefix", coverage, matched, {"prefix": True})

    # --- typo: trigram retrieval plus a one-edit token check ---
    # Multi-word queries must still match their words (see the guard below).
    if q_tris is None:
        try:
            q_tris = make_trigrams(query or " ".join(q_tokens), TRIGRAM_MAX)
        except Exception:
            q_tris = []
    try:
        sim = trigram_similarity(q_tris, doc.get("trigrams") or [])
    except Exception:
        sim = 0.0
    has_one_edit_token = any(
        _one_edit_apart(query_token, doc_token)
        for query_token in q_set
        for doc_token in title_set | aux_set
    )
    if sim >= TRIGRAM_MIN_SIM and has_one_edit_token:
        # A multi-word query only accepts a typo document that already matches
        # the minimum number of its words. Without this, "typing tips"
        # returned every "Setting Up"/"Writing Tips" video and "ready tying"
        # returned only noise: "setting", "writing" and "typing" are each one
        # edit apart, so a single near-miss was enough to qualify. Single-word
        # queries keep the pure typo tier ("instelar" still finds the film).
        if len(q_set) < 2 or matched >= min_most:
            return MatchInfo("typo", coverage, matched, {"trigram_sim": sim})

    # --- broad: only reachable when explicitly requested ---
    if allow_broad:
        return MatchInfo("broad", coverage, matched)
    # Does not qualify for any tier in the normal (precision-first) path.
    return MatchInfo(NONE_TIER, coverage, matched)


# ---------------------------------------------------------------------------
# Scoring (tier dominates; fine signals are tie-breakers)
# ---------------------------------------------------------------------------


def compute_search_score(
    doc: Dict[str, Any],
    tokens: Sequence[str],
    query: str,
    match: MatchInfo,
    q_tris: Optional[Sequence[str]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """Final relevance score for a doc.

    Score = tier priority + capped fine-grained tie-breakers. Fine signals
    (token count, quality/codec/year, filename, recency) are capped at
    FINE_SCORE_CAP so they never outrank a doc from a higher tier.
    """
    w = weights or {}
    score = match.priority

    fine = 0.0

    # token coverage within the tier (more tokens = better)
    fine += match.coverage * 6.0
    fine += min(match.matched, 3) * 1.5

    q_set = set(t.lower() for t in tokens if t)

    # quality / codec matches
    try:
        q_quality = q_set & set(
            str(t).lower() for t in (doc.get("quality_tokens") or [])
        )
        fine += len(q_quality) * float(w.get("quality", 1.0))
    except Exception:
        pass
    try:
        q_codec = q_set & set(str(t).lower() for t in (doc.get("codec_tokens") or []))
        fine += len(q_codec) * float(w.get("codec", 1.0))
    except Exception:
        pass

    # year match
    try:
        year = doc.get("year")
        if year is not None and any(str(year) == t for t in tokens):
            fine += float(w.get("year", 1.5))
    except Exception:
        pass

    # filename contains the full query (substring)
    try:
        q_lower = normalize_text(query)
        fname = normalize_text(doc.get("filename"))
        if q_lower and q_lower in fname:
            fine += float(w.get("filename", 1.0))
    except Exception:
        pass

    # small length penalty for very long filenames (kept tiny so it never
    # dominates a tier decision)
    try:
        fname_len = len(str(doc.get("filename") or ""))
        fine -= fname_len / 2000.0
    except Exception:
        pass

    # small recency tie-breaker (recent items slightly preferred)
    try:
        ts = doc.get("timestamp")
        if ts:
            if isinstance(ts, str):
                try:
                    ts_dt = datetime.fromisoformat(ts)
                except Exception:
                    ts_dt = None
            else:
                ts_dt = ts
            if ts_dt:
                age_seconds = (datetime.utcnow() - ts_dt).total_seconds()
                window = 30 * 24 * 3600  # 30 days
                recency = max(0.0, (window - age_seconds) / window)
                fine += recency * float(w.get("recency", 1.0))
    except Exception:
        pass

    return score + max(0.0, min(fine, FINE_SCORE_CAP))


# ---------------------------------------------------------------------------
# Word-family visibility
# ---------------------------------------------------------------------------

# A family hit ("read" -> "reading") is an equal match, but in a big library
# it is usually older than the literal hits, so it would sit on the last page.
# Lift whole family forms near the top (after a short run of literal matches).
# The budget is counted in documents, and forms are promoted from the rarest
# up: a form only enters when all of its hits fit, so a dominant stem
# ("reader" under "read") cannot consume the slots before "reading" does.
# SEARCH_FAMILY_SLOTS / SEARCH_FAMILY_AFTER tune the budget and the
# literal-run length.
FAMILY_PROMOTION_SLOTS = _env_int("SEARCH_FAMILY_SLOTS", 40)
FAMILY_PROMOTION_AFTER = _env_int("SEARCH_FAMILY_AFTER", 10)


def _family_forms(q_tokens: Iterable[str], doc: Dict[str, Any]) -> Tuple[str, ...]:
    """Family-form tokens the document carries for these query tokens.

    This intentionally does not exclude tokens the query matched exactly: a
    title like "Reading ... Read ..." belongs to the rare "reading" form even
    though "read" itself is a literal match.
    """
    q_set = {str(t).lower() for t in q_tokens if t}
    if not q_set:
        return ()
    doc_tokens = set(_doc_title_tokens(doc))
    doc_tokens |= {str(t).lower() for t in (doc.get("quality_tokens") or [])}
    doc_tokens |= {str(t).lower() for t in (doc.get("codec_tokens") or [])}
    return tuple(sorted({d for q in q_set for d in doc_tokens if _extends_query(q, d)}))


def promote_family_matches(
    results: List[Dict[str, Any]],
    tokens: Optional[Sequence[str]] = None,
    slots: Optional[int] = None,
    after: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Lift word-family hits near the top of an already-sorted list.

    Once a rare form is promoted, all of its hits travel together, so a
    search for "read" shows "reading" (and its duplicate copies) on the first
    page instead of on the last one. Forms too large to fit what is left of
    the budget keep their natural order. Everything else keeps its relative
    order, so pagination stays consistent across pages.
    """
    if not results:
        return results
    try:
        slots = FAMILY_PROMOTION_SLOTS if slots is None else int(slots)
        after = FAMILY_PROMOTION_AFTER if after is None else int(after)
    except Exception:
        return results
    if slots <= 0:
        return results

    family = [d for d in results if d.get("_family")]
    promoted: List[Dict[str, Any]] = []
    if tokens:
        form_docs: Dict[str, List[Dict[str, Any]]] = {}
        for doc in results:
            try:
                forms = _family_forms(tokens, doc)
            except Exception:
                forms = ()
            for form in forms:
                form_docs.setdefault(form, []).append(doc)
        promoted_ids = set()
        for _form, docs in sorted(
            form_docs.items(), key=lambda kv: (len(kv[1]), kv[0])
        ):
            new_docs = [d for d in docs if id(d) not in promoted_ids]
            # Whole forms only: skipping an oversized form keeps a common
            # stem from crowding out the rare variants behind it.
            if not new_docs or len(promoted) + len(new_docs) > slots:
                continue
            for doc in new_docs:
                promoted.append(doc)
                promoted_ids.add(id(doc))
    if not promoted:
        promoted = family[:slots]

    if not promoted:
        return results
    promoted_ids = {id(d) for d in promoted}
    rest = [d for d in results if id(d) not in promoted_ids]
    pos = max(0, min(after, len(rest)))
    return rest[:pos] + promoted + rest[pos:]


# ---------------------------------------------------------------------------
# Tier gating / broadening
# ---------------------------------------------------------------------------


def accepts_tier(tier: str, min_tier: str) -> bool:
    """True when `tier` is at least as precise as `min_tier`."""
    if tier not in TIER_PRIORITY:
        return False
    try:
        return TIER_ORDER.index(tier) <= TIER_ORDER.index(min_tier)
    except ValueError:
        return False


def resolve_min_tier(
    classified: Sequence[Dict[str, Any]], start_tier: Optional[str] = None
) -> str:
    """Walk the tier ladder from `start_tier` toward lower precision.

    Starting at `start_tier` (default SEARCH_MIN_TIER), accept the most
    precise tier that still accumulates >= MIN_RESULTS documents. Never
    broadens past "typo" — the regex/broad tier is intentionally never
    auto-selected in the normal path.
    """
    base = (start_tier or DEFAULT_MIN_TIER).strip().lower()
    if base not in TIER_PRIORITY:
        base = DEFAULT_MIN_TIER
    base_idx = TIER_ORDER.index(base)
    for tier in TIER_ORDER[base_idx:]:
        n = sum(1 for d in classified if accepts_tier(d.get("_tier", NONE_TIER), tier))
        if n >= MIN_RESULTS:
            return tier
    return "typo"


# ---------------------------------------------------------------------------
# Search quality logging (per real query — used to measure search health)
# ---------------------------------------------------------------------------


def log_search_quality(
    query: str,
    tokens: Sequence[str],
    results: Iterable[Dict[str, Any]],
    min_tier: str,
    source: str = "bot",
    fuzzy_used: bool = False,
    broad_used: bool = False,
) -> None:
    """Record one structured line per query for search-quality analysis.

    Emits: query, token count, result count, top/second scores, min tier,
    whether fuzzy or broad fallbacks were involved.
    """
    if not QUALITY_LOGGING:
        return
    try:
        scores = [float(r.get("_score") or 0.0) for r in (results or [])]
    except Exception:
        scores = []
    scores.sort(reverse=True)
    top = scores[0] if scores else 0.0
    second = scores[1] if len(scores) > 1 else 0.0
    logger.info(
        "search_quality source={} query={!r} tokens={} results={} top_score={:.2f} second_score={:.2f} min_tier={} fuzzy_used={} broad_used={}",
        source,
        query,
        len(list(tokens or [])),
        len(scores),
        top,
        second,
        min_tier,
        fuzzy_used,
        broad_used,
    )


# ---------------------------------------------------------------------------
# Candidate filtering helper (shared by bot + API)
# ---------------------------------------------------------------------------


def filter_by_tier(
    docs: Iterable[Dict[str, Any]],
    query: str,
    tokens: Sequence[str],
    q_tris: Optional[Sequence[str]] = None,
    start_tier: Optional[str] = None,
    allow_broad: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """Filter + annotate candidate docs by tier, broadening when scarce.

    Returns (accepted_docs, min_tier_used). Each accepted doc gets `_tier`
    and `_score` fields set from the shared engine. `min_tier_used` reflects
    the broadening decision so callers can log which tier was actually used.
    """
    q_tokens = [t.lower() for t in tokens if t]
    q_text = query or " ".join(q_tokens)
    if q_tris is None:
        try:
            q_tris = make_trigrams(q_text, TRIGRAM_MAX)
        except Exception:
            q_tris = []

    # classify everything first so we can decide how far to broaden
    classified = []
    for doc in docs or []:
        try:
            match = classify_match(
                q_text, q_tokens, doc, q_tris, allow_broad=allow_broad
            )
            if match.tier == NONE_TIER:
                continue
            doc["_tier"] = match.tier
            doc["_score"] = compute_search_score(doc, q_tokens, q_text, match, q_tris)
            classified.append(doc)
        except Exception:
            continue

    min_tier = resolve_min_tier(classified, start_tier=start_tier)
    accepted = [
        d for d in classified if accepts_tier(d.get("_tier", NONE_TIER), min_tier)
    ]
    return accepted, min_tier
