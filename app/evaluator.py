"""Feature-flag evaluation engine (pure, deterministic, dependency-free).

Stability contract
------------------
For a given (flag definition, user_id, attributes) the evaluation is a pure
function. Percentage rollouts use a SHA-1 hash of
``f"{flag_key}:{user_id}"`` mapped into [0, 100) ("stable hash bucket"), so:

  * the same user always lands in the same bucket for the same flag;
  * traffic grows monotonically as the configured percentage grows;
  * different flags distribute independently (the flag key salts the hash).

Because configs are stored as immutable versioned documents, pinning the
request to a version therefore guarantees repeatable results forever.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional


class FlagNotFound(Exception):
    def __init__(self, flag: str):
        self.flag = flag
        super().__init__(f"flag {flag!r} not found")


def hash_bucket(flag: str, user_id: str) -> int:
    """Return a stable integer bucket in [0, 100) for a flag/user pair."""
    digest = hashlib.sha1(f"{flag}:{user_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _as_iterable(value: Any) -> Optional[list[Any]]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return parts if len(parts) > 1 else [value]
    return None


def _compare(op: str, attr: Any, expected: Any) -> bool:
    try:
        if op == "eq":
            return attr == expected
        if op == "ne":
            return attr != expected
        if op == "in":
            items = _as_iterable(expected)
            return items is not None and attr in items
        if op == "nin":
            items = _as_iterable(expected)
            return items is not None and attr not in items
        if op == "gt":
            return attr is not None and attr > expected
        if op == "ge":
            return attr is not None and attr >= expected
        if op == "lt":
            return attr is not None and attr < expected
        if op == "le":
            return attr is not None and attr <= expected
        if op == "prefix":
            return isinstance(attr, str) and isinstance(expected, str) and attr.startswith(expected)
        if op == "suffix":
            return isinstance(attr, str) and isinstance(expected, str) and attr.endswith(expected)
    except TypeError:
        return False
    return False


def _match_rule(rule: dict[str, Any], attrs: dict[str, Any]) -> bool:
    # An empty condition list is a catch-all rule.
    conditions = rule.get("conditions") or []
    return all(
        _compare(c["op"], attrs.get(c["attr"]), c.get("value")) for c in conditions
    )


def _node(
    flag: str,
    value: Any,
    reason: str,
    *,
    rule_id: Optional[str] = None,
    rollout: Optional[dict[str, Any]] = None,
    prerequisite_of: Optional[str] = None,
    expected: Optional[bool] = None,
) -> dict[str, Any]:
    return {
        "flag": flag,
        "value": value,
        "hit": bool(value),
        "reason": reason,
        "rule_id": rule_id,
        "rollout": rollout,
        "prerequisite_of": prerequisite_of,
        "expected": expected,
    }


def _evaluate(
    flag_name: str,
    flags: dict[str, dict[str, Any]],
    attrs: dict[str, Any],
    chain: list[dict[str, Any]],
    visiting: set[str],
    memo: dict[str, tuple[Any, dict[str, Any]]],
    appended: set[str],
) -> tuple[Any, dict[str, Any]]:
    """Evaluate ``flag_name`` and return ``(value, own_node)``.

    Nodes of prerequisite flags are appended to ``chain`` by the caller that
    consumes them. The node for ``flag_name`` itself is returned (not
    appended) so each flag appears exactly once in the chain even when a
    transitive prerequisite fails or dependencies form a diamond; the
    ``memo`` cache ensures a shared prerequisite is evaluated only once.
    """
    flag = flags[flag_name]

    if flag_name in memo:
        return memo[flag_name]

    if flag_name in visiting:
        # Defensive: publishing rejects cycles, but never loop forever.
        node = _node(flag_name, False, "cycle_detected")
        memo[flag_name] = (False, node)
        return False, node

    visiting.add(flag_name)

    # 1) Prerequisites are evaluated depth-first and short-circuit.
    for prereq in flag.get("prerequisites", []) or []:
        dep = prereq["flag"]
        expect = prereq.get("expect", True)
        dep_value, dep_node = _evaluate(
            dep, flags, attrs, chain, visiting, memo, appended
        )
        if dep not in appended:
            dep_node["prerequisite_of"] = flag_name
            dep_node["expected"] = expect
            chain.append(dep_node)
            appended.add(dep)
        if bool(dep_value) != expect:
            visiting.discard(flag_name)
            node = _node(flag_name, False, "prerequisite_failed")
            memo[flag_name] = (False, node)
            return False, node

    # 2) Disabled flags short-circuit to their declared default.
    if not flag.get("enabled", True):
        visiting.discard(flag_name)
        value = flag.get("default", False)
        node = _node(flag_name, value, "disabled")
        memo[flag_name] = (value, node)
        return value, node

    # 3) Ordered rules — first match wins.
    for idx, rule in enumerate(flag.get("rules", []) or []):
        if _match_rule(rule, attrs):
            value = rule.get("value", True)
            visiting.discard(flag_name)
            node = _node(
                flag_name,
                value,
                "rule_match",
                rule_id=rule.get("id") or f"rule[{idx}]",
            )
            memo[flag_name] = (value, node)
            return value, node

    # 4) Stable percentage rollout keyed on user_id.
    rollout = flag.get("rollout")
    if rollout is not None:
        raw_user = attrs.get("user_id")
        user_id = "" if raw_user is None else str(raw_user)
        percentage = rollout.get("percentage", 0)
        bucket = hash_bucket(flag_name, user_id)
        rollout_info = {"bucket": bucket, "percentage": percentage}
        if user_id != "" and bucket < percentage:
            value = rollout.get("value", True)
            node = _node(flag_name, value, "rollout_in", rollout=rollout_info)
        else:
            value = flag.get("default", False)
            node = _node(flag_name, value, "rollout_out", rollout=rollout_info)
        visiting.discard(flag_name)
        memo[flag_name] = (value, node)
        return value, node

    # 5) Fallback to the declared default.
    value = flag.get("default", False)
    visiting.discard(flag_name)
    node = _node(flag_name, value, "default")
    memo[flag_name] = (value, node)
    return value, node


def evaluate_flag(
    flag_name: str,
    flags: dict[str, dict[str, Any]],
    *,
    user_id: Any = None,
    attributes: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Evaluate one flag and return its value plus the full dependency chain.

    The chain is leaf-first: prerequisite evaluations precede the flag that
    required them; the requested flag is always the last node and carries the
    top-level value/rule/rollout information. Each flag appears at most once
    (shared/diamond dependencies are memoized).
    """
    if flag_name not in flags:
        raise FlagNotFound(flag_name)

    attrs = dict(attributes or {})
    if user_id is not None and "user_id" not in attrs:
        attrs["user_id"] = user_id

    chain: list[dict[str, Any]] = []
    value, final = _evaluate(
        flag_name, flags, attrs, chain, set(), memo={}, appended=set()
    )
    chain.append(final)
    return {
        "flag": flag_name,
        "value": value,
        "hit": bool(value),
        "reason": final["reason"],
        "rule_id": final["rule_id"],
        "rollout": final["rollout"],
        "chain": chain,
    }
