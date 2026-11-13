"""Ask the Gumnut API for face links only on person-filtered asset reads.

Immich models people through faces; "Person filters" in
docs/architecture/adapter-architecture.md explains why. The API rejects
``person_link`` without a person filter, so send it only alongside one. The
pinned SDK has no typed ``person_link`` parameter, so it travels in
``extra_query``.
"""

from collections.abc import Mapping

FACE_PERSON_LINK_QUERY: Mapping[str, str] = {"person_link": "face"}
