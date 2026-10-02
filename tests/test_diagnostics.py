from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import server_context as ctx
from ast_parser import RpyParser
from lsprotocol import types

import diagnostics


URI = "file:///workspace/game/script.rpy"


def parse(source: str) -> RpyParser:
    parser = RpyParser(dedent(source).strip("\n") + "\n")
    parser.parse()
    return parser


def symbol_map(parser: RpyParser, getter) -> dict:
    result: dict = {}
    for node in getter(parser):
        result.setdefault(node.name, []).append((URI, node))
    return result


def patch_index(monkeypatch, parser: RpyParser, *, image_files=None) -> None:
    """Point the workspace getters at *parser*'s own symbols."""
    getters = {
        "_get_all_workspace_labels": RpyParser.get_all_labels,
        "_get_all_workspace_defines": RpyParser.get_all_defines,
        "_get_all_workspace_defaults": RpyParser.get_all_defaults,
        "_get_all_workspace_screens": RpyParser.get_all_screens,
        "_get_all_workspace_images": RpyParser.get_all_images,
        "_get_all_workspace_transforms": RpyParser.get_all_transforms,
        "_get_all_workspace_styles": RpyParser.get_all_styles,
    }
    for attr, getter in getters.items():
        monkeypatch.setattr(
            ctx, attr, lambda g=getter: symbol_map(parser, g)
        )
    monkeypatch.setattr(ctx, "_resolve_renpy_file", lambda *a, **k: None)
    monkeypatch.setattr(
        ctx, "_resolve_image_name_to_file", (image_files or {}).get
    )
    monkeypatch.setattr(ctx, "_all_workspace_python_names", lambda: set())
    monkeypatch.setattr(
        ctx, "_get_all_workspace_show_tags", lambda: set()
    )
    monkeypatch.setattr(
        ctx._workspace_index, "get_used_labels", lambda: set()
    )
    monkeypatch.setattr(ctx, "_same_file_uri", lambda a, b: a == b)


def collect(monkeypatch, source: str, **kwargs) -> list:
    text = __import__("textwrap").dedent(source).strip("\n") + "\n"
    parser = parse(source)
    patch_index(monkeypatch, parser, **kwargs)
    return diagnostics._collect_full_diagnostics(URI, parser, text)


def codes(diags) -> list:
    return [str(d.code) for d in diags]


def messages(diags) -> list:
    return [d.message for d in diags]


# ── existing checks keep working ─────────────────────────────────────────


def test_undefined_jump_and_call_labels_are_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            jump missing_place
            call gone_label
        """,
    )
    assert codes(diags).count("undefined-label") == 2


def test_unused_label_is_hinted_and_tagged(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            "hi"

        label lonely:
            "bye"
        """,
    )
    unused = [d for d in diags if str(d.code) == "unused-label"]
    assert len(unused) == 1
    assert unused[0].severity == types.DiagnosticSeverity.Hint
    assert types.DiagnosticTag.Unnecessary in unused[0].tags


def test_entry_labels_are_never_unused(monkeypatch) -> None:
    diags = collect(monkeypatch, "label start:\n    \"hi\"\n")
    assert "unused-label" not in codes(diags)


def test_missing_image_file_in_image_definition(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        'image bg = "images/nope.png"\nlabel start:\n    "hi"\n',
    )
    assert "missing-image-file" in codes(diags)


# ── new: unused define/default variables ─────────────────────────────────


def test_unused_define_is_hinted(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        'define lonely = Character("nobody")\nlabel start:\n    "hi"\n',
    )
    unused = [d for d in diags if str(d.code) == "unused-define"]
    assert len(unused) == 1
    assert unused[0].severity == types.DiagnosticSeverity.Hint
    assert types.DiagnosticTag.Unnecessary in unused[0].tags
    assert 'Variable "lonely"' in unused[0].message


def test_used_define_is_not_hinted(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        'define e = Character("Eileen")\nlabel start:\n    e "hi"\n',
    )
    assert "unused-define" not in codes(diags)


def test_define_used_in_other_file_is_not_hinted(monkeypatch, tmp_path) -> None:
    other = tmp_path / "other.rpy"
    other.write_text('label other:\n    $ lonely()\n', encoding="utf-8")
    monkeypatch.setattr(
        ctx, "_get_workspace_rpy_files", lambda: [str(other)]
    )

    def fake_parse(path):
        uri = ctx._uri_from_path(path)
        text = Path(path).read_text(encoding="utf-8")
        parser = RpyParser(text)
        parser.parse()
        with ctx._cache_lock:
            ctx._parse_cache[uri] = (hash(text), text, parser.root, parser)
        return uri, parser.root, parser

    monkeypatch.setattr(ctx, "_get_parse_for_file", fake_parse)
    diags = collect(
        monkeypatch,
        'define lonely = Character("nobody")\nlabel start:\n    "hi"\n',
    )
    assert "unused-define" not in codes(diags)


def test_dotted_and_underscore_defines_are_skipped(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        'define music.track = "a.ogg"\n'
        "define _internal = 1\n"
        "define save_name = \"save\"\n"
        'label start:\n    "hi"\n',
    )
    assert "unused-define" not in codes(diags)


# ── new: undefined image references ──────────────────────────────────────


def test_show_undefined_image_is_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            show eileen happy
        """,
    )
    img = [d for d in diags if str(d.code) == "undefined-image"]
    assert len(img) == 1
    assert 'Image "eileen happy"' in img[0].message


def test_image_defined_in_project_is_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        image eileen happy = "eileen_happy.png"

        label start:
            show eileen happy
        """,
    )
    assert "undefined-image" not in codes(diags)


def test_image_known_by_tag_prefix_is_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        image eileen = "eileen.png"

        label start:
            show eileen vhappy
            hide eileen
        """,
    )
    assert "undefined-image" not in codes(diags)


def test_builtin_images_are_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            scene black
            show window
        """,
    )
    assert "undefined-image" not in codes(diags)


def test_bare_scene_and_expression_show_are_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            scene
            show expression "eileen.png"
            show layer master
        """,
    )
    assert "undefined-image" not in codes(diags)


def test_scene_with_as_clause_strips_clause(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        image 便利店内 = "convenience.png"

        label start:
            scene 便利店内 as bg with fade
        """,
    )
    assert "undefined-image" not in codes(diags)


def test_hide_of_show_as_tag_is_not_reported(monkeypatch) -> None:
    """``hide <tag>`` targets a tag introduced by ``show X as tag``, not an
    image statement (regression: 'Image "chapter_show_display" is not
    defined')."""
    diags = collect(
        monkeypatch,
        """
        label begin:
            show expression chapter_show_display() as chapter_show_display with diss
            return

        label end:
            hide chapter_show_display with diss
            return
        """,
    )
    assert "undefined-image" not in codes(diags)


# ── new: undefined transform references ──────────────────────────────────


def test_at_undefined_transform_is_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            show eileen at zoomy
        """,
    )
    tr = [d for d in diags if str(d.code) == "undefined-transform"]
    assert len(tr) == 1
    assert 'Transform "zoomy"' in tr[0].message


def test_at_known_transforms_are_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        define slide_in = Transform(xalign=1.0)

        transform wobble:
            xzoom 1.0

        label start:
            show eileen at wobble, slide_in, left
            show lucy at Transform(xpos=0.5)
            scene bg at Position(xalign=0.1)
        """,
    )
    assert "undefined-transform" not in codes(diags)


def test_as_clause_is_not_treated_as_transform_name(monkeypatch) -> None:
    """``show e at left as tag`` must report only the transform itself,
    never the glued ``as`` clause (regression: 'pos_center as noel')."""
    diags = collect(
        monkeypatch,
        """
        transform pos_left:
            xalign 0.0

        label start:
            show fred_normal_happy at pos_left as fred with dissolve
        """,
    )
    assert "undefined-transform" not in codes(diags)


def test_undefined_transform_message_excludes_as_clause(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        label start:
            show noel_normal at pos_center as noel with dissolve
        """,
    )
    tr = [d for d in diags if str(d.code) == "undefined-transform"]
    assert len(tr) == 1
    assert tr[0].message == 'Transform "pos_center" is not defined in the project'


def test_at_call_with_kwargs_is_one_transform(monkeypatch) -> None:
    """``at fx_waves(dark=dark, lite=lite)`` is a single expression — its
    keyword arguments must not surface as transform names (regression:
    'Transform "lite=lite)" is not defined')."""
    diags = collect(
        monkeypatch,
        """
        label start:
            scene expression background at fx_waves(dark=dark, lite=lite) with trans
            show eileen at slide(1, 2), left with diss
        """,
    )
    assert "undefined-transform" not in codes(diags)


# ── new: undefined style parents ─────────────────────────────────────────


def test_style_unknown_parent_is_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        style my_button is no_such_style:
            xpadding 10
        """,
    )
    st = [d for d in diags if str(d.code) == "undefined-style"]
    assert len(st) == 1
    assert 'Style "no_such_style"' in st[0].message


def test_style_builtin_and_defined_parents_are_not_reported(monkeypatch) -> None:
    diags = collect(
        monkeypatch,
        """
        style base:
            xpadding 5

        style my_button is base:
            xpadding 10

        style other_button is button_text:
            xpadding 12
        """,
    )
    assert "undefined-style" not in codes(diags)


# ── diagnostics disabled shortcut is unchanged ───────────────────────────


def test_light_collector_reports_parser_errors_and_empty_blocks() -> None:
    parser = parse(
        """
        label start:
            show eileen:
        """
    )
    # show eileen: with no body → empty block error; feed parser an unknown
    # error manually to keep this independent of parser internals.
    parser.errors.append((3, "fake unknown line"))
    diags = diagnostics._collect_light_diagnostics(parser)
    assert "unknown-statement" in codes(diags)
    assert "empty-atl-block" in codes(diags)


# ── severity overrides & save re-run guard ───────────────────────────────


def _save_diag_settings() -> dict:
    return dict(ctx._settings["diagnostics"])


def _restore_diag_settings(state: dict) -> None:
    ctx._settings["diagnostics"].clear()
    ctx._settings["diagnostics"].update(state)


def test_severity_override_suppresses_checks(monkeypatch) -> None:
    state = _save_diag_settings()
    try:
        ctx._settings["diagnostics"]["severity"] = {"unused-label": "none"}
        diags = collect(
            monkeypatch,
            """
        label orphan:
            return
        """,
        )
        assert "unused-label" not in codes(diags)
    finally:
        _restore_diag_settings(state)


def test_severity_override_downgrades_severity(monkeypatch) -> None:
    state = _save_diag_settings()
    try:
        ctx._settings["diagnostics"]["severity"] = {
            "undefined-transform": "hint"
        }
        diags = collect(
            monkeypatch,
            """
        label start:
            show eileen at zoomy
        """,
        )
        tr = [d for d in diags if str(d.code) == "undefined-transform"]
        assert len(tr) == 1
        assert tr[0].severity == types.DiagnosticSeverity.Hint
    finally:
        _restore_diag_settings(state)


def test_full_diagnostics_needed_tracks_published_hash(monkeypatch) -> None:
    uri = "file:///workspace/game/guard.rpy"
    parser = parse("label start:\n    return\n")
    try:
        # Unknown document → a full pass is needed.
        assert diagnostics.full_diagnostics_needed(uri) is True

        # Simulate a published pass for the cached content.
        with ctx._cache_lock:
            ctx._parse_cache[uri] = (hash("text"), "text", None, parser)
        with diagnostics._diag_queue_lock:
            diagnostics._last_full_hash[uri] = hash("text")
        assert diagnostics.full_diagnostics_needed(uri) is False

        # Content changed → needed again.
        with ctx._cache_lock:
            ctx._parse_cache[uri] = (hash("text2"), "text2", None, parser)
        assert diagnostics.full_diagnostics_needed(uri) is True

        diagnostics.forget_document(uri)
        assert diagnostics.full_diagnostics_needed(uri) is True
    finally:
        with ctx._cache_lock:
            ctx._parse_cache.pop(uri, None)
        diagnostics.forget_document(uri)
