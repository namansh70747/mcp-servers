"""F8 — entity-graph corroboration. Optional (networkx); pure-dict fallback always works.

Builds a person↔company↔domain↔email↔social graph from all gathered evidence and boosts the
candidate that the most INDEPENDENT sources converge on. Without networkx it falls back to a
simple source-cardinality count, which gives the same ranking signal.
"""
from __future__ import annotations


def corroboration_scores(candidates: list[dict]) -> dict[str, float]:
    """Given finder candidates [{email, sources:[...], ...}], return {email: corroboration_score}.

    Score rewards an email cited by MORE DISTINCT source types (independent evidence) over one
    cited many times by a single source. Uses networkx centrality when available, else a direct
    distinct-source count.
    """
    if not candidates:
        return {}

    # distinct-source count (the robust core signal)
    base: dict[str, float] = {}
    for c in candidates:
        email = (c.get("email") or "").lower()
        if not email:
            continue
        srcs = set(c.get("sources") or ([c.get("source")] if c.get("source") else []))
        base[email] = float(len(srcs))

    try:
        import networkx as nx
    except Exception:
        return base

    # Build a bipartite-ish graph: emails ↔ source-nodes; degree centrality ≈ corroboration.
    try:
        g = nx.Graph()
        for c in candidates:
            email = (c.get("email") or "").lower()
            if not email:
                continue
            g.add_node(email, kind="email")
            srcs = set(c.get("sources") or ([c.get("source")] if c.get("source") else []))
            for s in srcs:
                snode = f"src:{s}"
                g.add_node(snode, kind="source")
                g.add_edge(email, snode)
        cent = nx.degree_centrality(g)
        return {n: round(cent.get(n, 0.0) * 100, 3)
                for n in g.nodes if g.nodes[n].get("kind") == "email"} or base
    except Exception:
        return base


def best_candidate(candidates: list[dict]) -> dict | None:
    """Return the candidate with the highest corroboration score (graph-aware), or None."""
    scores = corroboration_scores(candidates)
    if not scores:
        return None
    best_email = max(scores, key=lambda e: scores[e])
    for c in candidates:
        if (c.get("email") or "").lower() == best_email:
            return {**c, "corroboration_score": scores[best_email]}
    return None
