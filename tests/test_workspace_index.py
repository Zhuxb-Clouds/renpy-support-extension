from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from ast_parser import RpyParser
from workspace_index import WorkspaceIndex


def uri_from_path(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


def path_from_uri(uri: str) -> str:
    return unquote(urlparse(uri).path)


def test_workspace_index_aggregates_styles_and_other_symbols(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    script = game_dir / "script.rpy"
    script.write_text(
        """
define e = Character("Eileen")
default has_key = False
style menu_button is button:
    color "#ffffff"
transform bounce:
    alpha 0.0
screen inventory():
    text "Inventory"
label start:
    jump end
label end:
    return
""".lstrip(),
        encoding="utf-8",
    )

    parse_cache = {}
    path_to_uri = {}

    def normalize_path(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get_parse_for_file(filepath: str):
        uri = uri_from_path(filepath)
        text = Path(filepath).read_text(encoding="utf-8")
        parser = RpyParser(text)
        ast = parser.parse()
        parse_cache[uri] = (hash(text), text, ast, parser)
        path_to_uri[normalize_path(filepath)] = uri
        return uri, ast, parser

    server = SimpleNamespace(
        workspace=SimpleNamespace(
            folders={"root": SimpleNamespace(uri=uri_from_path(tmp_path))}
        )
    )
    index = WorkspaceIndex(
        server=server,
        parse_cache=parse_cache,
        cache_lock=threading.Lock(),
        path_to_uri=path_to_uri,
        path_from_uri_fn=path_from_uri,
        normalize_path_fn=normalize_path,
        get_parse_for_file_fn=get_parse_for_file,
    )

    styles = index.get_styles()
    transforms = index.get_transforms()
    labels = index.get_labels()

    assert list(styles) == ["menu_button"]
    assert styles["menu_button"][0][1].parent == "button"
    assert list(transforms) == ["bounce"]
    assert set(labels) == {"start", "end"}
    assert index.get_used_labels() == {"end"}
