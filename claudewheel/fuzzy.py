"""Score, rank, and highlight fuzzy matches between queries and option lists."""


def fuzzy_score(query: str, candidate: str) -> tuple[int, list[int]]:
    """Score: 100 exact, 80 prefix, 60 contains, 40 subsequence, 0 no match.

    Returns (score, matched_positions) where positions are indices in candidate
    that count as matched (for highlighting). Empty list if no match.
    """
    q = query.lower()
    c = candidate.lower()
    if q == c:
        # Exact: every position in candidate is "matched"
        return 100, list(range(len(candidate)))
    if c.startswith(q):
        # Prefix: the prefix region is the match
        return 80, list(range(len(q)))
    if q in c:
        # Contains: the contiguous region where q occurs in c
        start = c.find(q)
        return 60, list(range(start, start + len(q)))
    # Subsequence match: walk through c greedily matching q chars,
    # recording each matched index
    qi = 0
    positions: list[int] = []
    for i, ch in enumerate(c):
        if qi < len(q) and ch == q[qi]:
            positions.append(i)
            qi += 1
    if qi == len(q):
        return 40, positions
    return 0, []


def fuzzy_rank(query: str, candidates: list[str]) -> list[str]:
    """Return candidates sorted by fuzzy match score (best first). Excludes non-matches."""
    scored = [(fuzzy_score(query, c)[0], c) for c in candidates]
    scored = [(s, c) for s, c in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored]


def fuzzy_match_positions(query: str, candidate: str) -> list[int]:
    """Return the list of indices in `candidate` matched by `query`, or empty if no match."""
    _, positions = fuzzy_score(query, candidate)
    return positions
