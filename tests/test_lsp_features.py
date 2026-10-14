"""Index-backed LSP features: references, rename, go-to, index freshness,
cross-file refresh, and the asset/Python-definition caches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from lsprotocol import types

import lsp_server
import server_context as ctx
from ast_parser import RpyParser
from workspace_index import WorkspaceIndex, extract_references


def uri_of(path: Path) -> str:
    return ctx._uri_from_path(str(path))


class _Doc:
    def __init__(self, path: Path) -> None:
        self.source = path.read_text(encoding="utf-8")
        self.lines = self.source.splitlines(True)


class _FakeLS:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = SimpleNamespace(
            get_text_document=lambda uri: _Doc(Path(ctx._path_from_uri(uri))),
            text_documents={},
        )


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text(
        "label start:\n"
        "    call chapter1\n"
        "    jump .sub\n"
        "label .sub:\n"
        "    call screen inventory\n"
        "    jump chapter1\n",
        encoding="utf-8",
    )
    (game / "chapter.rpy").write_text(
        "define e = Character(\"Eileen\")\n"
        "label chapter1:\n"
        "    e \"Hi, chapter1 is (here\"\n"
        "    $ seen = renpy.seen_label(\"chapter1\")\n"
        "    return\n"
        "screen inventory():\n"
        "    textbutton \"Go\" action Jump(\"chapter1\")\n"
        "screen hud():\n"
        "    use inventory\n",
        encoding="utf-8",
    )
    with ctx._cache_lock:
        ctx._parse_cache.clear()
        ctx._path_to_uri.clear()
    server = SimpleNamespace(
        workspace=SimpleNamespace(
            folders={"root": SimpleNamespace(uri=uri_of(tmp_path))}
        )
    )
    index = WorkspaceIndex(
        server=server,
        parse_cache=ctx._parse_cache,
        cache_lock=ctx._cache_lock,
        path_to_uri=ctx._path_to_uri,
        path_from_uri_fn=ctx._path_from_uri,
        normalize_path_fn=ctx._normalize_path_key,
        get_parse_for_file_fn=ctx._get_parse_for_file,
        python_defs_fn=ctx._find_python_definitions_in_file,
    )
    monkeypatch.setattr(ctx, "_workspace_index", index)
    monkeypatch.setattr(ctx, "LSP_SERVER", server)
    monkeypatch.setattr(ctx, "_renpy_py_files", None)
    monkeypatch.setattr(
        ctx, "_get_parse", lambda uri, source=None: ctx._get_parse_for_file(
            ctx._path_from_uri(uri)
        )[1:]
    )
    return SimpleNamespace(root=tmp_path, game=game, index=index, ls=_FakeLS(tmp_path))


def _position(path: Path, needle: str, offset: int = 0) -> types.Position:
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        col = line.find(needle)
        if col >= 0:
            return types.Position(line=i, character=col + offset)
    raise AssertionError(needle)


def _spans(locations) -> set:
    return {
        (Path(ctx._path_from_uri(loc.uri)).name, loc.range.start.line,
         loc.range.start.character, loc.range.end.character)
        for loc in locations
    }


def test_extract_references_covers_statements_use_and_strings() -> None:
    parser = RpyParser(
        "label a:\n"
        "    jump b\n"
        "    call screen s\n"
        "    $ renpy.jump(\"c\")\n"
        "screen s():\n"
        "    use t\n"
        "    textbutton \"x\" action [Show(\"u\"), Call('d')]\n"
    )
    parser.parse()
    refs = {(r.kind, r.name, r.line) for r in extract_references(parser)}
    assert refs == {
        ("label", "b", 2), ("screen", "s", 3), ("label", "c", 4),
        ("screen", "t", 6), ("screen", "u", 7), ("label", "d", 7),
    }


def test_references_are_exact_and_include_action_strings(project) -> None:
    script = project.game / "script.rpy"
    params = types.ReferenceParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(script)),
        position=_position(script, "chapter1"),
        context=types.ReferenceContext(include_declaration=True),
    )
    spans = _spans(lsp_server.find_references(project.ls, params))
    assert spans == {
        ("chapter.rpy", 1, 6, 14),     # label chapter1
        ("script.rpy", 1, 9, 17),      # call chapter1
        ("script.rpy", 5, 9, 17),      # jump chapter1 (inside menu-less .sub)
        ("chapter.rpy", 6, 33, 41),    # action Jump("chapter1")
    }


def test_goto_on_label_definition_lists_usages_once(project) -> None:
    chapter = project.game / "chapter.rpy"
    params = types.DefinitionParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(chapter)),
        position=_position(chapter, "chapter1"),
    )
    locations = lsp_server.goto_definition(project.ls, params)
    assert len(locations) == 3 == len(_spans(locations))


def test_rename_label_edits_every_reference_exactly_once(project) -> None:
    chapter = project.game / "chapter.rpy"
    params = types.RenameParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(chapter)),
        position=_position(chapter, "chapter1", 2),
        new_name="intro",
    )
    edit = lsp_server.rename(project.ls, params)
    flat = {
        (Path(ctx._path_from_uri(u)).name, e.range.start.line, e.range.start.character)
        for u, edits in edit.changes.items()
        for e in edits
    }
    assert flat == {
        ("chapter.rpy", 1, 6), ("chapter.rpy", 6, 33),
        ("script.rpy", 1, 9), ("script.rpy", 5, 9),
    }
    assert sum(len(v) for v in edit.changes.values()) == 4  # no duplicates
    # dialogue text mentioning the name is untouched
    assert all(e.range.start.line != 2 for e in edit.changes[uri_of(chapter)])


def test_rename_local_label_renames_only_the_last_segment(project) -> None:
    script = project.game / "script.rpy"
    params = types.RenameParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(script)),
        position=_position(script, ".sub", 1),
        new_name="part",
    )
    edit = lsp_server.rename(project.ls, params)
    edits = edit.changes[uri_of(script)]
    assert sorted((e.range.start.line, e.range.start.character) for e in edits) == [
        (2, 10), (3, 7)
    ]
    assert {e.new_text for e in edits} == {"part"}


def test_rename_screen_includes_use_statements(project) -> None:
    chapter = project.game / "chapter.rpy"
    params = types.RenameParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(chapter)),
        position=_position(chapter, "inventory"),
        new_name="bag",
    )
    edit = lsp_server.rename(project.ls, params)
    lines = sorted(
        (Path(ctx._path_from_uri(u)).name, e.range.start.line)
        for u, edits in edit.changes.items() for e in edits
    )
    assert lines == [("chapter.rpy", 5), ("chapter.rpy", 8), ("script.rpy", 4)]


def test_rename_rejects_invalid_names(project) -> None:
    chapter = project.game / "chapter.rpy"
    params = types.RenameParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(chapter)),
        position=_position(chapter, "chapter1"),
        new_name="has space",
    )
    assert lsp_server.rename(project.ls, params) is None


def test_index_tracks_reparsed_buffers_and_reports_name_changes(project) -> None:
    index = project.index
    script = project.game / "script.rpy"
    uri = uri_of(script)
    index.get_labels()  # full index
    index._ready.set()
    changed = []
    index.on_symbols_changed = changed.append

    def reparse(text: str) -> None:
        parser = RpyParser(text)
        ast = parser.parse()
        with ctx._cache_lock:
            ctx._parse_cache[uri] = (hash(text), text, ast, parser)
        index.update_file(uri)

    text = script.read_text(encoding="utf-8")
    reparse("\n" + text)  # line shift only
    assert changed == []
    reparse(text + "label epilogue:\n    return\n")
    assert changed == [uri]
    assert "epilogue" in index.get_labels()


def test_ensure_current_is_constant_time_once_complete(project, monkeypatch) -> None:
    index = project.index
    index.get_labels()
    calls = []
    monkeypatch.setattr(index, "get_file_list", lambda: calls.append(1) or [])
    index.get_labels()
    index.get_screens()
    assert calls == []


def test_python_definitions_come_from_the_index(project) -> None:
    hits = ctx._find_python_var_across_workspace("seen")
    assert [(Path(ctx._path_from_uri(u)).name, ln) for u, ln, _c in hits] == [
        ("chapter.rpy", 4)
    ]


def test_resolve_renpy_file_uses_asset_index(project, monkeypatch) -> None:
    deep = project.game / "assets" / "bg"
    deep.mkdir(parents=True)
    (deep / "room.png").write_bytes(b"")
    hidden = project.root / ".git" / "bg"
    hidden.mkdir(parents=True)
    (hidden / "other.png").write_bytes(b"")
    monkeypatch.setattr(ctx, "_asset_files", None)
    monkeypatch.setattr(ctx, "_get_renpy_search_dirs", lambda: [str(project.game)])

    assert ctx._resolve_renpy_file("bg/room.png") == str(deep / "room.png")
    assert ctx._resolve_renpy_file("bg/other.png") is None  # hidden dirs skipped

    (deep / "late.png").write_bytes(b"")
    assert ctx._resolve_renpy_file("bg/late.png") is None  # cached index
    ctx.invalidate_asset_caches(str(deep / "late.png"))
    assert ctx._resolve_renpy_file("bg/late.png") == str(deep / "late.png")


def test_invalidate_asset_caches_rebuilds_the_image_cache(monkeypatch) -> None:
    monkeypatch.setattr(ctx, "_image_cache", {"stale": "x"})
    monkeypatch.setattr(ctx, "_image_cache_built", True)
    ctx.invalidate_asset_caches()
    assert ctx._image_cache_built is False and ctx._image_cache == {}
