from __future__ import annotations

from textwrap import dedent

from ast_parser import (
    Camera,
    RpyParser,
    StyleDef,
)


def parse(source: str) -> RpyParser:
    parser = RpyParser(dedent(source).strip() + "\n")
    parser.parse()
    return parser


def test_parser_collects_core_symbols_and_skips_screen_atl_bodies() -> None:
    parser = parse(
        """
        define e = Character("Eileen")
        define audio.theme = "audio/theme.ogg"
        default has_key = False

        image bg room = "images/bg_room.png"

        style menu_button is button:
            color "#ffffff"

        transform bounce(speed=(1, (2, 3))):
            alpha 0.0
            linear 0.25 alpha 1.0

        screen inventory(items=(1, (2, 3))):
            frame:
                text "Inventory"

        label start:
            camera at center with dissolve:
                linear 0.2 zoom 1.1

            for key, value in inventory.items():
                pass

            jump end

        label end:
            return
        """
    )

    assert parser.errors == []
    assert [d.name for d in parser.get_all_defines()] == ["e", "audio.theme"]
    assert [d.name for d in parser.get_all_defaults()] == ["has_key"]
    assert [img.name for img in parser.get_all_images()] == ["bg room"]
    assert [t.name for t in parser.get_all_transforms()] == ["bounce"]
    assert [s.name for s in parser.get_all_screens()] == ["inventory"]
    assert [s.name for s in parser.get_all_styles()] == ["menu_button"]
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "end"]
    assert [j.target for j in parser.get_all_jumps()] == ["end"]

    cameras = parser._collect(parser.root, Camera)
    assert len(cameras) == 1
    assert cameras[0].at_transform == "center"
    assert cameras[0].with_transition == "dissolve"


def test_parser_recovers_unknown_statement_without_losing_later_symbols() -> None:
    parser = parse(
        """
        label start:
            this is not valid renpy
            jump end

        style warning_text:
            color "#ff0000"

        label end:
            return
        """
    )

    assert parser.errors == [(2, "Unrecognized statement: this is not valid renpy")]
    assert [lb.name for lb in parser.get_all_labels()] == ["start", "end"]
    assert [j.target for j in parser.get_all_jumps()] == ["end"]
    assert parser._collect(parser.root, StyleDef)[0].name == "warning_text"
