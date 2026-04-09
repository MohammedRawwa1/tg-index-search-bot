import re
from typing import List, Dict, Any, Optional

# improved normalization/tokenization rules
# token patterns: prefer explicit quality/year tokens and alnum sequences
_sep_re = re.compile(r'[\.\-_\[\]\(\)]+')
_space_re = re.compile(r"\s+")
_camel_re = re.compile(r'(?<!^)(?=[A-Z])')
_token_re = re.compile(r'\d{3,4}p|\d{4}|[a-z0-9]+', re.IGNORECASE)

# common release/junk tokens to strip (keeps numeric tokens like 1080p)
_release_junk = set(
    [
        "bluray",
        "bdrip",
        "brrip",
        "dvd",
        "dvdrip",
        "web",
        "webrip",
        "web-dl",
        "xvid",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "hdrip",
        "aac",
        "ac3",
        "mp3",
        "remux",
        "proper",
        "yify",
        "ettv",
        "rarbg",
        "dvdr",
        "limited",
        "internal",
        "subbed",
        "dubbed",
        "repack",
        "hc",
        "hdr",
        "uhd",
        "720p",
        "1080p",
        "2160p",
    ]
)


def _split_camel(s: str) -> List[str]:
    # split camelCase and PascalCase
    parts = _camel_re.sub(" ", s).split()
    return parts


def normalize_and_classify(filename: str) -> Dict[str, Any]:
    """
    Produce structured tokens from a filename:
      - `title_tokens`: core words likely matching user queries
      - `quality_tokens`: tokens like 1080p, 720p
      - `codec_tokens`: codec identifiers
      - `year`: 4-digit year if present
      - `other`: leftover tokens

    Uses a conservative token regex to keep meaningful alnum tokens and
    explicit quality/year tokens.
    """
    base = filename.rsplit("/", 1)[-1]
    s = base.replace("\n", " ")
    s = _sep_re.sub(" ", s)
    s = " ".join(_split_camel(s))
    s = s.lower()
    s = _space_re.sub(" ", s).strip()

    # extract candidate tokens with the token regex
    parts = [p for p in _token_re.findall(s) if p]

    title_tokens: List[str] = []
    quality_tokens: List[str] = []
    codec_tokens: List[str] = []
    year: Optional[int] = None
    other: List[str] = []

    for p in parts:
        if not p:
            continue
        pl = p.lower()
        # detect 4-digit year
        if pl.isdigit() and len(pl) == 4 and 1900 <= int(pl) <= 2100:
            year = int(pl)
            continue
        # quality tokens like 1080p
        if re.match(r"^\d{3,4}p$", pl):
            quality_tokens.append(pl)
            continue
        # codec tokens
        if pl in ("x264", "x265", "h264", "h265", "hevc"):
            codec_tokens.append(pl)
            continue
        # known release/junk tokens: skip
        if pl in _release_junk:
            continue
        # simple alnum tokens are good title tokens
        if re.match(r"^[a-z0-9]+$", pl):
            title_tokens.append(pl)
            continue
        other.append(pl)

    def uniq(seq: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return {
        "title_tokens": uniq(title_tokens),
        "quality_tokens": uniq(quality_tokens),
        "codec_tokens": uniq(codec_tokens),
        "year": year,
        "other": uniq(other),
    }


def tokenize_filename(filename: str) -> Dict[str, Any]:
    return normalize_and_classify(filename)


def tokenize_query(query: str) -> List[str]:
    """Tokenize free text search queries using the same token regex."""
    q = query.replace("\n", " ")
    q = _sep_re.sub(" ", q)
    q = " ".join(_split_camel(q))
    q = q.lower().strip()
    tokens = [t.lower() for t in _token_re.findall(q) if t]
    return tokens