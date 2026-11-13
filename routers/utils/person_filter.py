"""Keep Immich person filters to photos that show the person's face.

The Gumnut API's person filter also matches photos where the person appears
only as a body, unless ``person_link=face`` narrows it. Immich models people
through faces: the person page offers face actions (feature photo, reassign,
move to a new person) that need a face on every photo it lists, and mobile
person pages are built from synced faces. So every person-filtered asset
read the adapter makes asks for face links only.

The Gumnut API rejects ``person_link`` without a person filter, so send it
only alongside one. The pinned SDK has no typed ``person_link`` parameter, so
it travels in ``extra_query``.
"""

from collections.abc import Mapping

FACE_PERSON_LINK_QUERY: Mapping[str, str] = {"person_link": "face"}
