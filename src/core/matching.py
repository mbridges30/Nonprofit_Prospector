"""
Fuzzy name matching for nonprofit entities.
Handles variations like "Ballmer Group" vs "Balmer Foundation Inc".
"""

import re
from thefuzz import fuzz


# Common suffixes and prefixes to strip before comparing
_STRIP_PATTERNS = [
    r'\b(inc|incorporated|corp|corporation|llc|ltd|limited)\b',
    r'\b(co|company)\b',
    r'\b(the|a|an)\b',
    r'[.,;:!?\'\"()\-]',
]

_COMPILED_STRIPS = [re.compile(p, re.IGNORECASE) for p in _STRIP_PATTERNS]


def normalize_name(name: str) -> str:
    """Normalize an entity name for comparison."""
    if not name:
        return ""
    s = name.lower().strip()
    for pat in _COMPILED_STRIPS:
        s = pat.sub(" ", s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def fuzzy_match(name1: str, name2: str, threshold: int = 70) -> bool:
    """Check if two entity names are a fuzzy match.

    Uses token_sort_ratio which handles word reordering
    (e.g., "Boys Girls Club" vs "Club Boys Girls").

    Args:
        name1: First entity name
        name2: Second entity name
        threshold: Minimum similarity score (0-100). Default 70.

    Returns:
        True if names match above threshold.
    """
    if not name1 or not name2:
        return False

    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    # Exact match after normalization
    if n1 == n2:
        return True

    # Check if one is a substring of the other (common for short names)
    # Require the shorter string to be a significant portion of the longer one
    # to avoid "William" matching "William F Halpin Math And Science Foundation"
    if len(n1) >= 4 and len(n2) >= 4:
        shorter = min(len(n1), len(n2))
        longer = max(len(n1), len(n2))
        if (n1 in n2 or n2 in n1) and shorter >= longer * 0.5:
            return True

    # Fuzzy match using token sort (handles word reordering)
    score = fuzz.token_sort_ratio(n1, n2)
    if score >= threshold:
        return True

    # Also try partial ratio for cases like "Ballmer Group" matching
    # "Steve A Ballmer and Connie B Ballmer Foundation"
    # But require both names to have at least 3 words to avoid
    # short name false positives like "Jing Yan" ~ "Storytelling Agency"
    if len(n1.split()) >= 3 and len(n2.split()) >= 3:
        partial = fuzz.partial_ratio(n1, n2)
        if partial >= max(threshold, 80):
            return True

    return False


def fuzzy_match_score(name1: str, name2: str) -> int:
    """Return a similarity score (0-100) between two entity names."""
    if not name1 or not name2:
        return 0

    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if n1 == n2:
        return 100

    return max(
        fuzz.token_sort_ratio(n1, n2),
        fuzz.partial_ratio(n1, n2),
    )
