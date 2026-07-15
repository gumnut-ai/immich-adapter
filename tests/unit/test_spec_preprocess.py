"""Unit tests for the generator's spec-preprocessing transform.

`tools/spec_preprocess.py` is kept dependency-free precisely so these pure-function
tests can import it without the model generator's PEP-723 inline deps (click/requests/
pyyaml). ``tools/`` is not an importable package (no ``__init__.py``, not on
``sys.path`` during tests), so we load the module by path.

Unlike ``test_immich_models.py`` (which locks the committed generated artifact), these
tests exercise the strip logic directly — including ``time``, a format the spec never
uses, so the artifact can't cover it.
"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "spec_preprocess.py"
_spec = importlib.util.spec_from_file_location("spec_preprocess", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
spec_preprocess = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spec_preprocess)
strip_non_string_patterns = spec_preprocess.strip_non_string_patterns


def _field(fmt: str) -> dict:
    return {"type": "string", "format": fmt, "pattern": "^x$"}


def test_strips_pattern_for_each_non_string_format():
    """Every format datamodel-codegen maps to a non-string type loses its pattern."""
    for fmt in ("uuid", "date-time", "date", "time"):
        node = _field(fmt)
        removed = strip_non_string_patterns(node)
        assert removed == 1, fmt
        assert "pattern" not in node, fmt
        assert node["format"] == fmt  # only `pattern` is dropped; `format` is retained


def test_keeps_pattern_on_genuine_string_fields():
    """A pattern on a plain string, or a string `format` like email, is left intact."""
    for schema in (
        {"type": "string", "pattern": "^x$"},
        {"type": "string", "format": "email", "pattern": "^x$"},
    ):
        removed = strip_non_string_patterns(schema)
        assert removed == 0
        assert schema["pattern"] == "^x$"


def test_dict_valued_format_key_does_not_crash():
    """A property literally named "format" whose value is a nested schema (unhashable)
    must be skipped by the membership test, not raise — the real spec has one."""
    node = {"format": {"type": "string", "pattern": "^keep$"}, "type": "object"}
    removed = strip_non_string_patterns(node)
    assert removed == 0
    assert node["format"]["pattern"] == "^keep$"  # recursed into; string pattern kept


def test_recurses_through_nested_dicts_and_lists_and_counts():
    """The walk reaches patterns nested under properties, array items, and allOf."""
    spec = {
        "components": {
            "schemas": {
                "Asset": {
                    "type": "object",
                    "properties": {
                        "id": _field("uuid"),
                        "createdAt": _field("date-time"),
                        "name": {"type": "string", "pattern": "^n$"},  # string: kept
                        "tags": {"type": "array", "items": _field("uuid")},  # list
                    },
                    "allOf": [_field("date")],  # list of schemas
                }
            }
        }
    }
    removed = strip_non_string_patterns(spec)

    schemas = spec["components"]["schemas"]["Asset"]
    props = schemas["properties"]
    assert removed == 4  # id + createdAt + tags.items + allOf[0]
    assert "pattern" not in props["id"]
    assert "pattern" not in props["createdAt"]
    assert "pattern" not in props["tags"]["items"]
    assert "pattern" not in schemas["allOf"][0]
    assert props["name"]["pattern"] == "^n$"  # genuine string field untouched
