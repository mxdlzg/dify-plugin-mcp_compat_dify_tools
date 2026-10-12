"""Resolve Dify endpoint tool-configuration values into plain values.

Dify stores endpoint tool settings as *expression objects* rather than raw
literals. A literal constant looks like ``{"type": "constant", "value": "false"}``.
Some Dify versions wrap the value one more layer. Values can also be dynamic
expressions (variable references / mixed templates) that need workflow context
we do not have inside an endpoint plugin, so they must be reported, never
guessed (taskbook Task 4).

Dify 1.17.x semantics for ``mixed``: every ``text-input``/``secret-input``
setting is stored as ``{"type": "mixed", "value": ...}`` whether or not the
text contains a variable reference. A mixed value WITHOUT any ``{{#...#}}``
reference is just fixed text (which may be an empty string, or even a
non-string scalar) and resolves to that literal; only a mixed value that
actually references workflow variables is unresolvable here.

This module is intentionally conservative: it only unwraps dicts that are
*definitely* Dify expression envelopes, so a legitimate business object that
happens to contain a ``value`` key is left untouched (principle 4).
"""

from __future__ import annotations

import re
from typing import Any


class UnresolvedParameterError(ValueError):
    """Raised when a config expression cannot be resolved without context."""


# Dify value-selector types that reference runtime state we cannot access in an
# endpoint. Resolving these requires workflow context, so we refuse instead of
# returning a wrong literal. ``mixed`` is NOT in this set: see module docstring.
_DYNAMIC_TYPES: frozenset[str] = frozenset({
    "variable",
    "var",
    "template",
})

# Expression types Dify's value-selector actually emits. Used to decide
# whether a single-key {"value": ...} dict is a wrapper or a legitimate
# business object ({"value": ..., "type": "money"} must never be unwrapped).
KNOWN_EXPRESSION_TYPES: frozenset[str] = (
    frozenset({"constant", "mixed"}) | _DYNAMIC_TYPES
)

# Dify template variable reference: {{#sys.query#}}, {{#1711528712102.text#}},
# {{#context#}}. Any {{#...#}} in the text means runtime substitution needed.
_VAR_REF_RE = re.compile(r"\{\{#[^{}]*#\}\}")


def references_variable(value: Any) -> bool:
    """True when a value contains a Dify ``{{#...#}}`` variable reference."""
    if isinstance(value, str):
        return _VAR_REF_RE.search(value) is not None
    if isinstance(value, dict):
        return any(references_variable(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(references_variable(v) for v in value)
    return False


def _is_expression_dict(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") in KNOWN_EXPRESSION_TYPES
    )


def _is_wrapped_expression(value: Any) -> bool:
    """True for the single-key wrapper Dify's getStructureValue() produces:

    ``{"value": {"type": "constant", "value": "false"}}``

    Deliberately narrow: exactly one ``value`` key whose content is itself an
    expression dict. A business object that carries ``value`` alongside other
    keys is not a wrapper (principle 4).
    """

    return (
        isinstance(value, dict)
        and set(value.keys()) == {"value"}
        and _is_expression_dict(value["value"])
    )


def resolve_config_value(value: Any) -> Any:
    """Resolve one Dify tool-configuration value to its concrete value.

    Handles constant literals and verified nested wrapping. Raises
    ``UnresolvedParameterError`` for dynamic expressions. Non-dict values and
    dicts that are not expression envelopes pass through unchanged.
    """

    # Bounded unwrap depth guards against a pathological self-referencing dict.
    depth = 0
    while depth < 32:
        # Dify's getStructureValue() wraps each settings entry as
        # {"value": {"type": "constant", "value": ...}}; peel the wrapper
        # before the expression itself is handled.
        if _is_wrapped_expression(value):
            value = value["value"]
            depth += 1
            continue

        if not _is_expression_dict(value):
            return value

        expr_type = value["type"]
        if expr_type in _DYNAMIC_TYPES:
            raise UnresolvedParameterError(
                f"config uses dynamic expression type '{expr_type}' "
                "which requires workflow context and is not supported here"
            )
        if expr_type == "mixed":
            if "value" not in value:
                raise UnresolvedParameterError(
                    "mixed expression is missing its 'value' field"
                )
            inner = value["value"]
            if references_variable(inner):
                raise UnresolvedParameterError(
                    "mixed expression references workflow variables "
                    "({{#...#}}) which are not available in an endpoint; "
                    "refusing to guess its value"
                )
            # Dify 1.17.1: text-input/secret-input without a {{#...#}}
            # reference is fixed text — resolve to the literal (kept as-is:
            # "" stays "", 0 stays 0).
            value = inner
            depth += 1
            continue
        if expr_type == "constant":
            if "value" not in value:
                raise UnresolvedParameterError(
                    "constant expression is missing its 'value' field"
                )
            value = value["value"]
            depth += 1
            continue
        # An expression dict with an unrecognised type: stop unwrapping and
        # hand the original dict back. Blindly taking "value" could destroy a
        # legitimate object, so we do not.
        return value
    return value


def resolve_config_map(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve a whole tool-configuration map (name -> expression) to values.

    Keys whose values cannot be resolved raise so the caller can decide whether
    to fail the tool call or skip the parameter with a diagnostic.
    """

    return {name: resolve_config_value(val) for name, val in config.items()}
