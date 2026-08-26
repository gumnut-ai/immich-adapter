"""Translate Immich favorite/rating filters to the Gumnut rating dial."""

from collections.abc import Mapping
from typing import Any


FAVORITE_RATING = 5


def rating_filter_value(
    *, rating: int | None = None, is_favorite: bool | None = None
) -> int | None:
    """Return the exact Gumnut rating selected by an Immich request.

    Immich's finer-grained ``rating`` filter wins when both fields are present.
    ``isFavorite=False`` is the clients' normal unfiltered/default shape, so only
    true selects the favorite rating.
    """
    if rating is not None:
        return rating
    if is_favorite is True:
        return FAVORITE_RATING
    return None


def rating_extra_query(
    rating: int | None, base: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Merge a rating into SDK ``extra_query`` request options.

    The deployed Gumnut API accepts ``ratings`` on asset list/count/search, but
    the generated Python SDK does not yet expose those typed parameters. Keep
    the compatibility shim in one place so it can be removed when generation
    catches up.
    """
    query = dict(base or {})
    if rating is not None:
        query["ratings"] = str(rating)
    return query or None
