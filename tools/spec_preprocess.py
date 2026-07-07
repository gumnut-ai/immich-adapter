"""Preprocessing for the Immich OpenAPI spec before pydantic-model codegen.

Kept dependency-free (stdlib only) so the pure transforms can be unit-tested without
the model generator's PEP-723 inline deps (click/requests/pyyaml).
"""

# OpenAPI `format` values that datamodel-codegen maps to a non-string Python type
# (uuid -> UUID, date-time -> AwareDatetime, date -> date, time -> time). pydantic-core
# rejects a string-only `pattern` constraint on any of these. Extend this set if a
# future spec trips the same error for another non-string format.
_NON_STRING_PATTERN_FORMATS = frozenset({"uuid", "date-time", "date", "time"})


def strip_non_string_patterns(node: object) -> int:
    """Remove ``pattern`` from schemas whose ``format`` maps to a non-string type.

    datamodel-codegen maps ``format: uuid`` to ``UUID``, ``date-time`` to
    ``AwareDatetime``, ``date`` to ``date`` and ``time`` to ``time``, then copies the
    (string-only) ``pattern`` constraint onto that non-string field. pydantic-core
    rejects a ``pattern`` constraint on such a schema with a ``TypeError`` when a value
    for the field is validated — so every generated DTO carrying a populated id or
    timestamp is un-constructable. The regex is a redundant re-encoding of what
    ``format`` already guarantees, so drop it before codegen sees the spec while leaving
    ``pattern`` on genuine string fields intact.

    Mutates ``node`` in place; returns the number of patterns removed.
    """
    removed = 0
    if isinstance(node, dict):
        # `format` is normally a string, but a property literally named "format" can map
        # to a nested schema (dict) — guard so the set membership never hashes a dict.
        fmt = node.get("format")
        if (
            isinstance(fmt, str)
            and fmt in _NON_STRING_PATTERN_FORMATS
            and "pattern" in node
        ):
            del node["pattern"]
            removed += 1
        for value in node.values():
            removed += strip_non_string_patterns(value)
    elif isinstance(node, list):
        for item in node:
            removed += strip_non_string_patterns(item)
    return removed
