from __future__ import annotations

from textwrap import dedent

from ast_parser import (
    Camera,
    HideScreen,
    RpyParser,
    Show,
    ShowScreen,
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


def test_show_as_clause_is_not_part_of_at_transform() -> None:
    parser = parse(
        """
        label start:
            show noel normal at pos_center as noel with dissolve
            show eileen at left, right as e2 onlayer master
            show eileen as e3 at left
        """
    )

    shows = parser._collect(parser.root, Show)
    assert [s.at_transform for s in shows] == ["pos_center", "left, right", "left"]


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


def test_show_and_hide_screen_statements() -> None:
    parser = parse(
        """
        label start:
            show screen twitter_feed with dissolve
            show screen hud(align=(0.5, 0.5))
            hide screen twitter_feed with dissolve
        """
    )

    shows = parser._collect(parser.root, ShowScreen)
    assert [s.screen_name for s in shows] == ["twitter_feed", "hud"]
    assert shows[0].with_transition == "dissolve"
    assert shows[1].arguments == "align=(0.5, 0.5)"

    hides = parser._collect(parser.root, HideScreen)
    assert [h.screen_name for h in hides] == ["twitter_feed"]
    assert hides[0].with_transition == "dissolve"
