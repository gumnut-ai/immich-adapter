"""Translate Immich favorite/rating filters to the Gumnut rating dial."""

from collections.abc import Mapping
from typing import Any


FAVORITE_RATING = 5
NON_FAVORITE_RATINGS = frozenset(range(FAVORITE_RATING))
RatingFilter = frozenset[int]


def rating_filter_values(
    *,
    rating: int | None = None,
    rating_provided: bool = False,
    is_favorite: bool | None = None,
) -> RatingFilter | None:
    """Return the exact Gumnut rating set selected by an Immich request.

    Immich's finer-grained ``rating`` filter wins when both fields are present.
    An explicitly-present null selects unrated assets, represented by backend
    rating 0; an omitted rating adds no restriction.
    Explicit favorite true selects rating 5; false selects every non-favorite
    rating, including backend rating 0's unrated cohort. Omission adds no
    favorite restriction.
    """
    if rating is not None:
        return frozenset({rating})
    if rating_provided:
        return frozenset({0})
    if is_favorite is True:
        return frozenset({FAVORITE_RATING})
    if is_favorite is False:
        return NON_FAVORITE_RATINGS
    return None


def rating_extra_query(
    ratings: RatingFilter | None, base: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Merge exact ratings into SDK ``extra_query`` request options.

    The deployed Gumnut API accepts ``ratings`` on asset list/count/search, but
    the generated Python SDK does not yet expose those typed parameters. Keep
    the compatibility shim in one place so it can be removed when generation
    catches up.
    """
    query = dict(base or {})
    if ratings is not None:
        query["ratings"] = ",".join(str(rating) for rating in sorted(ratings))
    return query or None
