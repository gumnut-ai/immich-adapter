"""Tests for edit-base selection over the asset version chain."""

import logging
from dataclasses import dataclass

import pytest

from routers.utils.asset_version_chain import (
    InvalidVersionChainError,
    is_edit_version,
    select_edit_base,
)


@dataclass
class Version:
    id: str
    position: int
    kind: str


def version(position: int, kind: str) -> Version:
    return Version(id=f"asset_version_{position}", position=position, kind=kind)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("edit", True),
        ("edit:immich", True),
        ("original", False),
        ("external:enhancer", False),
        ("editorial", False),
        ("something-new", False),
    ],
)
def test_is_edit_version(kind: str, expected: bool) -> None:
    assert is_edit_version(version(1, kind)) is expected


def test_root_only_chain_selects_root() -> None:
    root = version(0, "original")
    assert select_edit_base([root], asset_id="asset_1") is root


def test_edits_are_skipped() -> None:
    root = version(0, "original")
    chain = [version(2, "edit"), root, version(1, "edit")]
    assert select_edit_base(chain, asset_id="asset_1") is root


def test_latest_external_beats_root_and_later_edit() -> None:
    external = version(1, "external:enhancer")
    chain = [version(0, "original"), external, version(2, "edit")]
    assert select_edit_base(chain, asset_id="asset_1") is external


def test_unknown_kind_is_a_valid_base() -> None:
    unknown = version(1, "something-new")
    chain = [version(0, "original"), unknown]
    assert select_edit_base(chain, asset_id="asset_1") is unknown


def test_edit_below_external_base_selects_external(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Externals derive from the whole chain below them, so this is a normal chain.
    external = version(2, "external:enhancer")
    chain = [version(0, "original"), version(1, "edit"), external]
    with caplog.at_level(logging.WARNING, logger="routers.utils.asset_version_chain"):
        assert select_edit_base(chain, asset_id="asset_1") is external
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "chain",
    [
        [],
        [version(1, "original")],
        [version(0, "original"), version(0, "original")],
    ],
)
def test_missing_or_duplicate_root_rejected(chain: list[Version]) -> None:
    with pytest.raises(InvalidVersionChainError):
        select_edit_base(chain, asset_id="asset_1")


def test_chain_with_no_non_edit_version_rejected() -> None:
    with pytest.raises(InvalidVersionChainError):
        select_edit_base([version(0, "edit")], asset_id="asset_1")
