from __future__ import annotations

from textwrap import dedent

import lsp_server
from ast_parser import RpyParser


URI = "file:///workspace/game/script.rpy"


def parse(source: str) -> tuple[list[str], RpyParser]:
    text = dedent(source).strip("\n") + "\n"
    parser = RpyParser(text)
    parser.parse()
    return text.splitlines(), parser


def labels(items) -> set[str]:
    return {item.label for item in items}


def symbol_map(nodes):
    result = {}
    for node in nodes:
        result.setdefault(node.name, []).append((URI, node))
    return result


def patch_workspace_symbols(monkeypatch, parser: RpyParser) -> None:
    monkeypatch.setattr(
        lsp_server, "_get_all_workspace_labels", lambda: symbol_map(parser.get_all_labels())
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_defines",
        lambda: symbol_map(parser.get_all_defines()),
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_defaults",
        lambda: symbol_map(parser.get_all_defaults()),
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_screens",
        lambda: symbol_map(parser.get_all_screens()),
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_images",
        lambda: symbol_map(parser.get_all_images()),
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_transforms",
        lambda: symbol_map(parser.get_all_transforms()),
    )
    monkeypatch.setattr(
        lsp_server,
        "_get_all_workspace_styles",
        lambda: symbol_map(parser.get_all_styles()),
    )


def complete(lines: list[str], parser: RpyParser, line_no: int):
    return lsp_server._completion_items_for_context(
        URI, parser, lines, line_no, len(lines[line_no])
    )


def test_label_completion_keeps_trailing_space_context(monkeypatch) -> None:
    lines, parser = parse(
        """
        label start:
            jump 

        label ending:
            return
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    items = complete(lines, parser, 1)

    assert "ending" in labels(items)
    assert "jump" not in labels(items)


def test_completion_suggests_variables_styles_transforms_and_screens(monkeypatch) -> None:
    lines, parser = parse(
        """
        define e = Character("Eileen")
        define audio.theme = "audio/theme.ogg"
        default has_key = False
        image bg room = "images/bg_room.png"
        style menu_button is button:
            color "#ffffff"
        transform bounce:
            alpha 0.0
        screen inventory():
            text "Inventory"

        label start:
            $ 
            show bg room at 
            call screen 
            style 
            play music 
        """
    )
    patch_workspace_symbols(monkeypatch, parser)

    general = labels(complete(lines, parser, 12))
    assert {"e", "audio.theme", "has_key", "start"} <= general

    transforms = labels(complete(lines, parser, 13))
    assert {"center", "bounce"} <= transforms

    screens = labels(complete(lines, parser, 14))
    assert "inventory" in screens

    styles = labels(complete(lines, parser, 15))
    assert "menu_button" in styles

    audio = labels(complete(lines, parser, 16))
    assert "theme" in audio


def test_completion_inside_screen_transform_and_style_blocks() -> None:
    text = (
        "screen hud():\n"
        "    \n"
        "\n"
        "transform fade_in:\n"
        "    \n"
        "\n"
        "style menu_button:\n"
        "    \n"
    )
    parser = RpyParser(text)
    parser.parse()
    lines = text.splitlines()

    screen_items = labels(complete(lines, parser, 1))
    assert {"text", "button", "xalign"} <= screen_items

    transform_items = labels(complete(lines, parser, 4))
    assert {"linear", "alpha", "xalign"} <= transform_items

    style_items = labels(complete(lines, parser, 7))
    assert {"color", "font", "hover_color"} <= style_items
