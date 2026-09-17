"""Semantic validation for uploaded flag configurations.

The validator is intentionally framework-free (plain dicts in, exception out)
so it can be unit-tested without FastAPI and reused by any client.

Checks performed:
  * rollout percentage must be an int in [0, 100], rollout key must be
    ``user_id`` (the only stable identity supported);
  * condition operators must be known;
  * rule ids must be unique within a flag;
  * prerequisites must point at flags that exist;
  * the prerequisite graph must be acyclic.
"""

from __future__ import annotations

from typing import Any

KNOWN_OPS = {"eq", "ne", "in", "nin", "gt", "ge", "lt", "le", "prefix", "suffix"}


class ConfigValidationError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def _err(errors: list[dict[str, Any]], flag: str, code: str, message: str) -> None:
    errors.append({"flag": flag, "code": code, "message": message})


def _detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return a list of cycles found in ``graph`` (each as a flag list).

    Classic DFS with a grey/black colour set. A back edge to a grey node
    closes a cycle; the cycle slice is reconstructed from the DFS stack.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = GREY
        stack.append(node)
        for dep in graph.get(node, ()):  # edges to missing flags were checked earlier
            if dep not in color:
                continue
            if color[dep] == GREY:
                idx = stack.index(dep)
                cycles.append(stack[idx:] + [dep])
            elif color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node)
    return cycles


def validate_config(config: dict[str, Any]) -> None:
    """Raise :class:`ConfigValidationError` if ``config`` is invalid."""
    errors: list[dict[str, Any]] = []

    flags = config.get("flags")
    if not isinstance(flags, dict):
        raise ConfigValidationError(
            [{"flag": None, "code": "invalid_config", "message": "config must contain a 'flags' object"}]
        )

    graph: dict[str, list[str]] = {}

    for name, flag in flags.items():
        if not isinstance(flag, dict):
            _err(errors, name, "invalid_flag", "flag definition must be an object")
            continue

        graph[name] = []

        rollout = flag.get("rollout")
        if rollout is not None:
            if not isinstance(rollout, dict):
                _err(errors, name, "invalid_rollout", "rollout must be an object")
            else:
                pct = rollout.get("percentage")
                if isinstance(pct, bool) or not isinstance(pct, int):
                    _err(
                        errors,
                        name,
                        "invalid_percentage",
                        f"rollout.percentage must be an integer, got {pct!r}",
                    )
                elif not 0 <= pct <= 100:
                    _err(
                        errors,
                        name,
                        "invalid_percentage",
                        f"rollout.percentage must be between 0 and 100, got {pct}",
                    )
                key = rollout.get("key", "user_id")
                if key != "user_id":
                    _err(
                        errors,
                        name,
                        "invalid_rollout_key",
                        f"rollout.key only supports 'user_id', got {key!r}",
                    )

        seen_rule_ids: set[str] = set()
        for idx, rule in enumerate(flag.get("rules", []) or []):
            if not isinstance(rule, dict):
                _err(errors, name, "invalid_rule", f"rules[{idx}] must be an object")
                continue
            rule_id = rule.get("id")
            if rule_id is not None:
                if not isinstance(rule_id, str) or not rule_id:
                    _err(errors, name, "invalid_rule_id", f"rules[{idx}].id must be a non-empty string")
                elif rule_id in seen_rule_ids:
                    _err(errors, name, "duplicate_rule_id", f"duplicate rule id {rule_id!r}")
                else:
                    seen_rule_ids.add(rule_id)
            for c_idx, cond in enumerate(rule.get("conditions", []) or []):
                if not isinstance(cond, dict):
                    _err(errors, name, "invalid_condition", f"rules[{idx}].conditions[{c_idx}] must be an object")
                    continue
                op = cond.get("op")
                if op not in KNOWN_OPS:
                    _err(
                        errors,
                        name,
                        "unknown_operator",
                        f"rules[{idx}].conditions[{c_idx}] uses unknown operator {op!r};"
                        f" allowed: {', '.join(sorted(KNOWN_OPS))}",
                    )

        prereqs = flag.get("prerequisites", []) or []
        for p_idx, prereq in enumerate(prereqs):
            if not isinstance(prereq, dict):
                _err(errors, name, "invalid_prerequisite", f"prerequisites[{p_idx}] must be an object")
                continue
            dep = prereq.get("flag")
            if dep == name:
                _err(errors, name, "self_dependency", f"flag {name!r} declares itself as a prerequisite")
                continue
            if dep not in flags:
                _err(
                    errors,
                    name,
                    "missing_prerequisite",
                    f"prerequisite {dep!r} (prerequisites[{p_idx}]) does not exist",
                )
                continue
            graph[name].append(dep)

    if not any(e["code"] == "missing_prerequisite" for e in errors):
        cycles = _detect_cycles(graph)
        for cycle in cycles:
            _err(
                errors,
                cycle[0],
                "circular_dependency",
                "circular prerequisite chain: " + " -> ".join(cycle),
            )

    if errors:
        raise ConfigValidationError(errors)
