"""Preserve every non-dependency field of a candidate Python manifest."""

import copy
import json
import sys
import tomllib


def configuration(text: str) -> dict:
    value = copy.deepcopy(tomllib.loads(text))
    project = value.get("project", {})
    project.pop("dependencies", None)
    project.pop("optional-dependencies", None)
    value.pop("dependency-groups", None)
    return value


old, candidate = json.load(sys.stdin)
if configuration(old) != configuration(candidate):
    raise SystemExit("non-dependency/supply-chain/test/type configuration changed")
