"""Nothing the adapter sends can carry a face template.

A face template is the biometric data derived from a face: its embedding, the
facial landmarks, and a person's centroid. The Gumnut API never returns one, and
Immich clients need only a face's bounding box and the person it belongs to. This
test keeps the adapter from becoming the place one leaks: it fails if any schema
the adapter publishes gains a field named for a template, or an array of numbers
that is not listed here with what it holds.

Faces reach Immich clients over REST routes, the sync stream and WebSocket
events. Only the first is in the OpenAPI document, so the scan reads two sources:

- the whole OpenAPI document, for the REST routes;
- every pydantic model the adapter defines, for the sync stream rows and the
  WebSocket payloads.

What this cannot see is anything a schema does not spell out: a free-form `dict`
or untyped field, which several upstream Immich DTOs have; what is written into a
plain string or a map of numbers; and a route that declares no response model or returns a raw
response. The adapter has no face data of its own to put there. Everything it
knows about a face comes from the Gumnut API, which has the same check on its
own responses.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from main import app
from routers.immich_models import (
    AssetFaceResponseDto,
    PersonResponseDto,
    SyncAssetFaceV1,
    SyncAssetFaceV2,
    SyncPersonV1,
)
from services.websockets import AssetUploadReadyV1Payload

REPO_ROOT = Path(__file__).resolve().parents[2]

# Words that name a face template. Compared against whole words of a property
# name, so `faceEmbedding` matches and `includeEmbeddedVideos` does not.
# "Descriptor" is another common name for a face embedding. The last four are
# the points a set of landmarks is made of (`left_eye`, `nose`, ...), which
# catches landmarks sent under a field name that says nothing.
TEMPLATE_WORDS = frozenset(
    {
        "embedding",
        "embeddings",
        "landmark",
        "landmarks",
        "centroid",
        "centroids",
        "descriptor",
        "descriptors",
        "keypoint",
        "keypoints",
        "eye",
        "nose",
        "mouth",
    }
)

# Arrays of numbers that upstream Immich defines, and what they hold. Add one
# only after checking that it cannot hold a face template.
NUMBER_ARRAYS = {
    "TimeBucketAssetResponseDto.latitude": "one coordinate per asset in the bucket",
    "TimeBucketAssetResponseDto.longitude": "one coordinate per asset in the bucket",
    "TimeBucketAssetResponseDto.ratio": "one aspect ratio per asset in the bucket",
    "TimeBucketAssetResponseDto.localOffsetHours": "one UTC offset per asset",
    "TimeBucketAssetResponseDto.duration": "one video length per asset",
    "AdminConfigFFmpegRealtimeDto.resolutions": "video heights to transcode to",
    "UserConfigFFmpegRealtimeDto.resolutions": "video heights to transcode to",
    "ValidationError.loc": "where in a rejected request the problem is",
    "NumberFilter.in": "search values the client sends",
    "NumberFilter.notIn": "search values the client sends",
    "NumberFilterNullable.in": "search values the client sends",
    "NumberFilterNullable.notIn": "search values the client sends",
}

# Schemas some routes name by reference without the document defining them: an
# upload request body and the bytes of a file. There is nothing to read at the
# reference; the model scan reads the adapter's models directly.
REFERENCES_THE_DOCUMENT_DOES_NOT_DEFINE = {
    "#/components/schemas/AssetMediaCreateDto",
    "#/components/schemas/BinaryFile",
}

# The scan is only worth trusting if it read the models that describe a face,
# and a WebSocket payload, which the OpenAPI document does not list.
MODELS_THE_SCAN_MUST_REACH: list[type[BaseModel]] = [
    AssetFaceResponseDto,
    PersonResponseDto,
    SyncAssetFaceV1,
    SyncAssetFaceV2,
    SyncPersonV1,
    AssetUploadReadyV1Payload,
]

_WORD_BOUNDARY = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
_COMBINATORS = ("anyOf", "oneOf", "allOf")
_NUMERIC_TYPES = frozenset({"number", "integer"})


def names_face_template(property_name: str) -> bool:
    words = _WORD_BOUNDARY.split(property_name)
    return any(word.lower() in TEMPLATE_WORDS for word in words)


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any] | None:
    target: Any = root
    for part in node["$ref"].removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    return target


def _types(node: dict[str, Any]) -> set[str]:
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    return set(declared or ())


def _admits_number(node: Any, root: dict[str, Any], open_refs: frozenset[str]) -> bool:
    """Whether a value matching `node` can be a number or hold only numbers."""
    if not isinstance(node, dict):
        return False
    if "$ref" in node:
        if node["$ref"] in open_refs:
            return False
        return _admits_number(_resolve(node, root), root, open_refs | {node["$ref"]})
    if _types(node) & _NUMERIC_TYPES:
        return True
    if any(
        _admits_number(option, root, open_refs)
        for key in _COMBINATORS
        for option in node.get(key, ())
    ):
        return True
    # A list of lists of numbers holds points as well as a list holds a vector.
    return any(
        _admits_number(item, root, open_refs)
        for item in (node.get("items"), *node.get("prefixItems", ()))
    )


def _walk(
    node: Any, root: dict[str, Any], field: str, open_refs: frozenset[str]
) -> Iterator[tuple[str, str]]:
    """Yield `(field, reason)` for each place under `node` that could carry a template.

    `field` names a property by the model that declares it, `PersonResponseDto.name`.
    """
    if not isinstance(node, dict):
        return
    if "$ref" in node:
        target = _resolve(node, root)
        if target is None:
            yield node["$ref"], "reference to a schema that is not defined"
        # A recursive model reaches itself again; its fields are already walked.
        elif node["$ref"] not in open_refs:
            yield from _walk(target, root, field, open_refs | {node["$ref"]})
        return

    for key in _COMBINATORS:
        for option in node.get(key, ()):
            yield from _walk(option, root, field, open_refs)

    model = node.get("title", "(untitled)")
    for name, subschema in (node.get("properties") or {}).items():
        property_field = f"{model}.{name}"
        if names_face_template(name):
            yield property_field, "property name names a face template"
        yield from _walk(subschema, root, property_field, open_refs)

    element_schemas = [node.get("items"), *node.get("prefixItems", ())]
    if any(_admits_number(item, root, open_refs) for item in element_schemas):
        yield field, "array of numbers"
    for item in element_schemas:
        yield from _walk(item, root, field, open_refs)

    yield from _walk(node.get("additionalProperties"), root, field, open_refs)


def find_face_template_carriers(
    schema: dict[str, Any], document: dict[str, Any] | None = None
) -> dict[str, set[str]]:
    """Each field in `schema` that could carry a face template, with the reasons.

    References resolve against `document`, or against `schema` itself.
    """
    carriers: dict[str, set[str]] = {}
    root = schema if document is None else document
    for field, reason in _walk(schema, root=root, field="", open_refs=frozenset()):
        carriers.setdefault(field, set()).add(reason)
    return carriers


def _schemas_in(node: Any) -> Iterator[dict[str, Any]]:
    """Every schema an OpenAPI path item holds: parameters, bodies and responses."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "schema" and isinstance(value, dict):
                yield value
            else:
                yield from _schemas_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schemas_in(value)


def scan_openapi_document(document: dict[str, Any]) -> dict[str, set[str]]:
    carriers: dict[str, set[str]] = {}
    schemas = [
        *document.get("components", {}).get("schemas", {}).values(),
        *_schemas_in(document.get("paths", {})),
    ]
    for schema in schemas:
        for field, reasons in find_face_template_carriers(schema, document).items():
            carriers.setdefault(field, set()).update(reasons)
    return carriers


def _subclasses(model: type[BaseModel]) -> Iterator[type[BaseModel]]:
    for subclass in model.__subclasses__():
        yield subclass
        yield from _subclasses(subclass)


def adapter_models() -> list[type[BaseModel]]:
    """Every pydantic model defined in the adapter's own source, tests excluded.

    Importing `main` loads every router and service, so each model that can be
    sent is a `BaseModel` subclass by the time this runs.
    """
    models: list[type[BaseModel]] = []
    for model in set(_subclasses(BaseModel)):
        source = getattr(sys.modules.get(model.__module__), "__file__", None)
        if source is None:
            continue
        path = Path(source).resolve()
        if not path.is_relative_to(REPO_ROOT):
            continue
        inside = path.relative_to(REPO_ROOT).parts
        if "site-packages" in inside or inside[0] in {"tests", ".venv"}:
            continue
        models.append(model)
    return models


def scan_models(models: list[type[BaseModel]]) -> dict[str, set[str]]:
    carriers: dict[str, set[str]] = {}
    for model in models:
        schema = TypeAdapter(model).json_schema(mode="serialization")
        for field, reasons in find_face_template_carriers(schema).items():
            carriers.setdefault(field, set()).update(reasons)
    return carriers


def _unexpected(carriers: dict[str, set[str]]) -> dict[str, list[str]]:
    return {
        field: sorted(reasons)
        for field, reasons in carriers.items()
        if not (
            (reasons == {"array of numbers"} and field in NUMBER_ARRAYS)
            or field in REFERENCES_THE_DOCUMENT_DOES_NOT_DEFINE
        )
    }


def test_no_rest_schema_carries_a_face_template():
    document = app.openapi()
    operations = [
        operation for path in document["paths"].values() for operation in path.values()
    ]

    carriers = scan_openapi_document(document)

    # The adapter serves about 220 operations. Far fewer means the document was
    # not built from the real routes.
    assert len(operations) > 100, "the OpenAPI document lists too few routes to trust"
    assert not _unexpected(carriers), (
        "A schema the adapter publishes could carry a face template (embedding, "
        "landmarks or centroid), which must never be sent to a client. Remove "
        "or retype the field. Only an array of numbers can be excused, by "
        f"listing it in NUMBER_ARRAYS with what it holds: {_unexpected(carriers)}"
    )


def test_no_adapter_model_carries_a_face_template():
    models = adapter_models()

    carriers = scan_models(models)

    assert set(MODELS_THE_SCAN_MUST_REACH) <= set(models)
    assert not _unexpected(carriers), (
        "A model the adapter can send could carry a face template (embedding, "
        "landmarks or centroid), which must never be sent to a client. Remove "
        "or retype the field. Only an array of numbers can be excused, by "
        f"listing it in NUMBER_ARRAYS with what it holds: {_unexpected(carriers)}"
    )


def test_every_listed_array_of_numbers_still_exists():
    """A stale entry would excuse a future field of the same name."""
    found = scan_openapi_document(app.openapi()).keys() | scan_models(adapter_models())

    assert set(NUMBER_ARRAYS) <= found


class _SyncFaceWithEmbedding(SyncAssetFaceV2):
    embedding: list[float]


class _SyncFaceWithRenamedVector(SyncAssetFaceV2):
    features: list[float]


class _SyncFaceWithPointPairs(SyncAssetFaceV2):
    pointPairs: list[tuple[int, int]]


class _Point(BaseModel):
    x: int
    y: int


class _SyncFaceWithLandmarksUnderAnotherName(SyncAssetFaceV2):
    geometry: dict[str, _Point]
    leftEye: _Point


class _PersonWithCentroid(BaseModel):
    faceCentroidId: str


class _PageOfFaces(BaseModel):
    faces: list[_SyncFaceWithEmbedding]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            _SyncFaceWithEmbedding,
            {
                "_SyncFaceWithEmbedding.embedding": {
                    "array of numbers",
                    "property name names a face template",
                }
            },
        ),
        (
            _SyncFaceWithRenamedVector,
            {"_SyncFaceWithRenamedVector.features": {"array of numbers"}},
        ),
        (
            _SyncFaceWithPointPairs,
            {"_SyncFaceWithPointPairs.pointPairs": {"array of numbers"}},
        ),
        (
            # A map of points with free keys is one of the shapes this cannot see.
            _SyncFaceWithLandmarksUnderAnotherName,
            {
                "_SyncFaceWithLandmarksUnderAnotherName.leftEye": {
                    "property name names a face template"
                }
            },
        ),
        (
            _PersonWithCentroid,
            {
                "_PersonWithCentroid.faceCentroidId": {
                    "property name names a face template"
                }
            },
        ),
        (
            _PageOfFaces,
            {
                "_SyncFaceWithEmbedding.embedding": {
                    "array of numbers",
                    "property name names a face template",
                }
            },
        ),
        (SyncAssetFaceV2, {}),
    ],
)
def test_scan_finds_each_way_a_model_could_carry_a_template(
    model: type[BaseModel], expected: dict[str, set[str]]
):
    assert scan_models([model]) == expected


def test_scan_reads_the_schemas_an_openapi_document_holds():
    face = TypeAdapter(_SyncFaceWithEmbedding).json_schema(mode="serialization")
    document = {
        "paths": {
            "/faces": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Page"}
                                }
                            }
                        }
                    }
                },
                "post": {
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "title": "Inline",
                                        "type": "object",
                                        "properties": {"landmarks": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    }
                },
            }
        },
        "components": {
            "schemas": {
                "Page": {
                    "title": "Page",
                    "type": "object",
                    "properties": {
                        "faces": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Face"},
                        }
                    },
                },
                "Face": face,
            }
        },
    }

    assert scan_openapi_document(document) == {
        "_SyncFaceWithEmbedding.embedding": {
            "array of numbers",
            "property name names a face template",
        },
        "Inline.landmarks": {"property name names a face template"},
    }


@pytest.mark.parametrize(
    "name",
    [
        "embedding",
        "faceEmbeddings",
        "face_landmarks",
        "centroidId",
        "descriptor",
        "leftEye",
        "mouth_right",
    ],
)
def test_template_words_are_matched_in_camel_and_snake_case(name: str):
    assert names_face_template(name)


@pytest.mark.parametrize(
    "name",
    ["boundingBoxX1", "includeEmbeddedVideos", "storageTemplate", "thumbnailPath"],
)
def test_other_property_names_are_not_matched(name: str):
    assert not names_face_template(name)
