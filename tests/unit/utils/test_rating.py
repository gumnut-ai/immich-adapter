"""Tests for favorite/rating filter translation."""

import pytest

from routers.utils.rating import rating_extra_query, rating_filter_values


@pytest.mark.parametrize(
    ("rating", "is_favorite", "expected"),
    [
        (None, None, None),
        (None, False, frozenset({0, 1, 2, 3, 4})),
        (None, True, frozenset({5})),
        (3, None, frozenset({3})),
        (3, True, frozenset({3})),
    ],
)
def test_rating_filter_values(rating, is_favorite, expected):
    assert rating_filter_values(rating=rating, is_favorite=is_favorite) == expected


def test_explicit_null_rating_selects_unrated_and_wins_over_favorite():
    assert rating_filter_values(rating_provided=True) == frozenset({0})
    assert rating_filter_values(rating_provided=True, is_favorite=True) == frozenset(
        {0}
    )


def test_rating_extra_query_merges_without_mutating_base():
    base = {"local_datetime_after": "2024-01-01"}

    result = rating_extra_query(frozenset({5}), base)

    assert result == {
        "local_datetime_after": "2024-01-01",
        "ratings": "5",
    }
    assert base == {"local_datetime_after": "2024-01-01"}


def test_rating_extra_query_omits_empty_query():
    assert rating_extra_query(None) is None


def test_rating_extra_query_serializes_multiple_values_stably():
    assert rating_extra_query(frozenset({4, 0, 2, 1, 3})) == {"ratings": "0,1,2,3,4"}
