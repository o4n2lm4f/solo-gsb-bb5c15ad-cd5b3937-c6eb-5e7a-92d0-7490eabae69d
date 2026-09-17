"""Version comparison and reverse-dependency impact analysis.

Pure, framework-free functions (plain dicts in, plain dicts out) so the logic
can be unit-tested without FastAPI, mirroring ``validation.py``/``evaluator.py``.

Definitions
-----------
* A flag is **directly changed** between two versions when it was added,
  removed, or its normalized definition differs. Definitions are compared as
  stored: both versions went through the same request-model normalization and
  canonical JSON round-trip, so plain structural equality is a normalized
  comparison.
* The dependency graph is the **union of the prerequisite edges of both
  versions**. Impact propagates along the *reverse* edges: if ``b`` declares
  ``a`` as a prerequisite, a change in ``a`` impacts ``b`` and, transitively,
  everything that depends on ``b``. The union of two acyclic graphs may
  contain cycles; the traversal below tolerates that.
* For every impacted flag a single shortest path from the changed flag to the
  dependent is reported; equal-length ties resolve to the lexicographically
  smallest path. Flag names and paths are sorted lexicographically and each
  impacted flag appears at most once per changed flag.
"""

from __future__ import annotations

import heapq
from typing import Any


def diff_flags(
    from_flags: dict[str, dict[str, Any]],
    to_flags: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(added, removed, modified)`` flag names, each sorted."""
    from_names = set(from_flags)
    to_names = set(to_flags)
    added = sorted(to_names - from_names)
    removed = sorted(from_names - to_names)
    modified = sorted(
        name for name in from_names & to_names if from_flags[name] != to_flags[name]
    )
    return added, removed, modified


def build_reverse_graph(*flag_sets: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Union of prerequisite edges, reversed: prerequisite -> its dependents."""
    dependents: dict[str, set[str]] = {}
    for flags in flag_sets:
        for name, spec in flags.items():
            dependents.setdefault(name, set())
            for prereq in spec.get("prerequisites", []) or []:
                dep = prereq.get("flag")
                if dep is None:
                    continue
                dependents.setdefault(dep, set()).add(name)
    return {node: sorted(targets) for node, targets in dependents.items()}


def shortest_paths_from(
    source: str, adj: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Lexicographically smallest shortest path from ``source`` to each node.

    Returns ``{node: [source, ..., node]}`` for every node reachable from
    ``source`` (excluding ``source`` itself). Dijkstra over the priority
    ``(distance, path)``: the first time a node is popped its path is shortest,
    and among the shortest ones lexicographically smallest.
    """
    best: dict[str, list[str]] = {}
    heap: list[tuple[int, tuple[str, ...]]] = [(0, (source,))]
    while heap:
        dist, path = heapq.heappop(heap)
        node = path[-1]
        if node in best:
            continue
        best[node] = list(path)
        for nxt in adj.get(node, ()):
            if nxt not in best:
                heapq.heappush(heap, (dist + 1, path + (nxt,)))
    del best[source]
    return best


def impact_analysis(
    changed_flags: list[str], adj: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Group reverse-reachable dependents per directly changed flag."""
    impacts: list[dict[str, Any]] = []
    for name in sorted(set(changed_flags)):
        paths = shortest_paths_from(name, adj)
        impacts.append(
            {
                "flag": name,
                "impacted": [
                    {"flag": dep, "path": paths[dep]} for dep in sorted(paths)
                ],
            }
        )
    return impacts


def compare_versions(
    from_flags: dict[str, dict[str, Any]],
    to_flags: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Full comparison: name diff plus reverse-dependency impact analysis."""
    added, removed, modified = diff_flags(from_flags, to_flags)
    adj = build_reverse_graph(from_flags, to_flags)
    impacts = impact_analysis(added + removed + modified, adj)
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "impacts": impacts,
    }
