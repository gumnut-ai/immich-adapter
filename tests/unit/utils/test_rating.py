"""Tests for favorite/rating filter translation."""

import pytest

from routers.utils.rating import rating_extra_query, rating_filter_value


@pytest.mark.parametrize(
    ("rating", "is_favorite", "expected"),
    [
        (None, None, None),
        (None, False, None),
        (None, True, 5),
        (3, None, 3),
        (3, True, 3),
    ],
)
def test_rating_filter_value(rating, is_favorite, expected):
    assert rating_filter_value(rating=rating, is_favorite=is_favorite) == expected


def test_rating_extra_query_merges_without_mutating_base():
    base = {"local_datetime_after": "2024-01-01"}

    result = rating_extra_query(5, base)

    assert result == {
        "local_datetime_after": "2024-01-01",
        "ratings": "5",
    }
    assert base == {"local_datetime_after": "2024-01-01"}


def test_rating_extra_query_omits_empty_query():
    assert rating_extra_query(None) is None
