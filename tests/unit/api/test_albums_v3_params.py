"""Immich v3 query-param conformance for the albums asset-add endpoints.

Immich v3 dropped the `key`/`slug` query params from `PUT /albums/{id}/assets`
and `PUT /albums/assets`, while KEEPING them on `GET /albums/{id}` (v3 dropped
only `withoutAssets` there). These guard that intentional asymmetry.

`routers/api/albums.py` cannot be imported on the `migration/immichv3` branch —
it imports `Error1`, which the v3 model regen removed, so `inspect.signature`
(which requires importing the module) is unavailable here. Parsing the source
with `ast` inspects the signatures without executing the module.
"""

import ast
from pathlib import Path

_ALBUMS_SRC = Path(__file__).resolve().parents[3] / "routers" / "api" / "albums.py"


def _param_names(func_name: str) -> set[str]:
    tree = ast.parse(_ALBUMS_SRC.read_text())
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == func_name
    )
    return {a.arg for a in node.args.args + node.args.kwonlyargs}


def test_put_album_assets_dropped_key_and_slug():
    """PUT /albums/{id}/assets no longer declares key/slug."""
    params = _param_names("add_assets_to_album")
    assert "key" not in params
    assert "slug" not in params


def test_put_albums_assets_dropped_key_and_slug():
    """PUT /albums/assets no longer declares key/slug."""
    params = _param_names("add_assets_to_albums")
    assert "key" not in params
    assert "slug" not in params


def test_get_album_info_retains_key_and_slug():
    """GET /albums/{id} keeps key/slug in v3 (only withoutAssets was dropped)."""
    params = _param_names("get_album_info")
    assert "key" in params
    assert "slug" in params
