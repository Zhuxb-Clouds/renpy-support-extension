"""Index-backed LSP features: references, rename, go-to, index freshness,
cross-file refresh, and the asset/Python-definition caches."""

from __future__ import annotations

import struct
import zlib
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


# ── Workspace symbols ────────────────────────────────────────────────────


def test_workspace_symbol_search_finds_labels_and_defines(project) -> None:
    hits = lsp_server.workspace_symbol(
        project.ls, types.WorkspaceSymbolParams(query="chapter")
    )
    names = {s.name for s in hits}
    assert "chapter1" in names

    everything = lsp_server.workspace_symbol(
        project.ls, types.WorkspaceSymbolParams(query="")
    )
    by_name = {s.name: s for s in everything}
    assert "chapter1" in by_name
    assert "e" in by_name
    assert by_name["chapter1"].container_name.endswith(".rpy")


# ── Call hierarchy ───────────────────────────────────────────────────────


def _prepare(project, path: Path, needle: str):
    items = lsp_server.prepare_call_hierarchy(
        project.ls,
        types.CallHierarchyPrepareParams(
            text_document=types.TextDocumentIdentifier(uri=uri_of(path)),
            position=_position(path, needle),
        ),
    )
    assert items, needle
    return items[0]


def test_call_hierarchy_incoming_and_outgoing(project) -> None:
    script = project.game / "script.rpy"
    chapter = project.game / "chapter.rpy"

    start = _prepare(project, script, "label start")
    chapter1 = _prepare(project, chapter, "label chapter1")

    incoming = lsp_server.incoming_calls(
        project.ls, types.CallHierarchyIncomingCallsParams(item=chapter1)
    )
    callers = {c.from_.name for c in incoming}
    assert callers == {"start", "start.sub"}

    outgoing = lsp_server.outgoing_calls(
        project.ls, types.CallHierarchyOutgoingCallsParams(item=start)
    )
    targets = {c.to.name for c in outgoing}
    assert targets == {"chapter1", "start.sub"}


def test_call_hierarchy_prepare_on_reference_resolves_target(project) -> None:
    script = project.game / "script.rpy"
    items = lsp_server.prepare_call_hierarchy(
        project.ls,
        types.CallHierarchyPrepareParams(
            text_document=types.TextDocumentIdentifier(uri=uri_of(script)),
            position=_position(script, "call chapter1", offset=5),
        ),
    )
    assert items
    assert items[0].name == "chapter1"


# ── Media preview hover ──────────────────────────────────────────────────


def _hover_at(project, path: Path, line_no: int):
    params = types.HoverParams(
        text_document=types.TextDocumentIdentifier(uri=uri_of(path)),
        position=types.Position(line=line_no, character=4),
    )
    return lsp_server.hover(project.ls, params)


def _png_bytes(width: int, height: int) -> bytes:
    """Minimal valid PNG header carrying the given dimensions."""
    ihdr = struct.pack(">II5B", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def test_media_hover_image_and_audio(project) -> None:
    game = project.game
    (game / "images").mkdir()
    (game / "images" / "bg_room.png").write_bytes(_png_bytes(128, 64))
    (game / "audio").mkdir()
    (game / "audio" / "oldtime.ogg").write_bytes(b"OggS")
    media = game / "media.rpy"
    media.write_text(
        'image bg room = "images/bg_room.png"\n'
        'define audio.oldtime = "audio/oldtime.ogg"\n'
        "label main:\n"
        "    show bg room\n"
        "    play music oldtime\n"
        '    play sound "audio/oldtime.ogg"\n',
        encoding="utf-8",
    )
    project.index._rpy_files = None  # force re-glob for the new file

    image_hover = _hover_at(project, media, 3)
    assert image_hover is not None
    value = image_hover.contents.value
    assert "![preview](file://" in value
    # 128x64 already fits the default 400px box: no upscaling
    assert "| width=128, height=64)" in value

    define_hover = _hover_at(project, media, 4)
    assert define_hover is not None
    assert "command:renpy-lsp.previewMedia" in define_hover.contents.value

    direct_hover = _hover_at(project, media, 5)
    assert direct_hover is not None
    assert "command:renpy-lsp.previewMedia" in direct_hover.contents.value


def test_media_hover_image_contained_to_max_size(project) -> None:
    game = project.game
    (game / "images").mkdir()
    (game / "images" / "bg_big.png").write_bytes(_png_bytes(800, 600))
    (game / "images" / "bg_fake.png").write_bytes(b"\x89PNG\r\n")
    media = game / "media3.rpy"
    media.write_text(
        'image bg big = "images/bg_big.png"\n'
        'image bg fake = "images/bg_fake.png"\n'
        "label main3:\n"
        "    show bg big\n"
        "    show bg fake\n",
        encoding="utf-8",
    )
    project.index._rpy_files = None

    big_hover = _hover_at(project, media, 3)
    assert big_hover is not None
    # 800x600 scales down to the default 400px box, ratio preserved
    assert "| width=400, height=300)" in big_hover.contents.value

    # unreadable dimensions: width-only fallback (height scales in the renderer)
    fake_hover = _hover_at(project, media, 4)
    assert fake_hover is not None
    assert "| width=400)" in fake_hover.contents.value

    old = ctx._settings["previews"]["maxSize"]
    try:
        ctx._settings["previews"]["maxSize"] = 200
        smaller_hover = _hover_at(project, media, 3)
        assert smaller_hover is not None
        assert "| width=200, height=150)" in smaller_hover.contents.value
    finally:
        ctx._settings["previews"]["maxSize"] = old


def test_media_hover_disabled_by_setting(project) -> None:
    media = project.game / "media2.rpy"
    media.write_text(
        'image bg room = "images/bg_room.png"\n'
        "label main2:\n"
        "    show bg room\n",
        encoding="utf-8",
    )
    project.index._rpy_files = None
    with ctx._settings_lock if hasattr(ctx, "_settings_lock") else ctx._cache_lock:
        old = ctx._settings["previews"]["enabled"]
        ctx._settings["previews"]["enabled"] = False
    try:
        hover = _hover_at(project, media, 2)
        assert hover is None or "![preview]" not in hover.contents.value
    finally:
        ctx._settings["previews"]["enabled"] = old


def test_media_hover_and_definition_inside_play_file_list(project) -> None:
    game = project.game
    (game / "audio").mkdir(exist_ok=True)
    (game / "audio" / "page_turn.ogg").write_bytes(b"OggS")
    media = game / "media_list.rpy"
    media.write_text(
        'define sfx_page_turn = "audio/page_turn.ogg"\n'
        "label media_list:\n"
        '    play sound_0 [ sfx_page_turn, "<silence 5>" ] loop\n',
        encoding="utf-8",
    )
    project.index._rpy_files = None  # force re-glob for the new file

    # Hover on the bare variable inside the list → audio preview link.
    hover = lsp_server.hover(
        project.ls,
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=uri_of(media)),
            position=_position(media, "sound_0 [ sfx_page_turn", offset=14),
        ),
    )
    assert hover is not None
    assert "command:renpy-lsp.previewMedia" in hover.contents.value

    # Go-to-definition on the same item → the define.
    defs = lsp_server.goto_definition(
        project.ls,
        types.DefinitionParams(
            text_document=types.TextDocumentIdentifier(uri=uri_of(media)),
            position=_position(media, "sound_0 [ sfx_page_turn", offset=14),
        ),
    )
    assert defs
    assert "media_list.rpy" in defs[0].uri
