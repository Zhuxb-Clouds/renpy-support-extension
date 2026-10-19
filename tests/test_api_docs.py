from __future__ import annotations

import importlib.util
from pathlib import Path

import api_docs


def load_fetch_api():
    """Import scripts/fetch_api.py (not a package) as a module."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "fetch_api", root / "scripts" / "fetch_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_file_extracts_all_directive_kinds(tmp_path) -> None:
    fetch = load_fetch_api()
    entries: dict = {}
    fetch.parse_file(
        "sample.rst",
        "\n".join(
            [
                "Sample",
                "======",
                "",
                ".. var:: config.screen_width = 800",
                "",
                "    The virtual width of the game, in ``pixels``.",
                "",
                ".. function:: renpy.pause(delay=0,",
                "        absolute=True)",
                "",
                "    Stops the game for *delay* seconds.",
                "",
                ".. style-property:: kerning float",
                "",
                "    A kerning adjustment, in pixels.",
                "",
            ]
        ),
        entries,
    )

    assert entries["config.screen_width"]["kind"] == "var"
    assert entries["config.screen_width"]["signature"] == "= 800"
    assert "pixels" in entries["config.screen_width"]["desc"]

    assert entries["renpy.pause"]["kind"] == "function"
    assert entries["renpy.pause"]["signature"].startswith("(delay=0")
    assert "Stops the game" in entries["renpy.pause"]["desc"]

    assert entries["kerning"]["kind"] == "style-property"
    assert entries["kerning"]["signature"] == "float"
    assert entries["kerning"]["file"] == "sample"


def test_parse_file_keeps_documented_entry_over_undocumented(tmp_path) -> None:
    fetch = load_fetch_api()
    entries: dict = {}
    fetch.parse_file(
        "a.rst",
        ".. var:: config.debug = True\n\n    Debug mode.\n",
        entries,
    )
    # Same name again, no description — must not shadow the real doc.
    fetch.parse_file("b.rst", ".. var:: config.debug\n", entries)
    assert entries["config.debug"]["desc"] == "Debug mode."
    assert entries["config.debug"]["file"] == "a"


def test_bundled_database_has_core_entries() -> None:
    # The committed renpy_api_docs.json must cover the common lookups.
    assert api_docs.lookup("config.screen_width")
    assert api_docs.lookup("kerning")
    assert api_docs.lookup("gui.text_size")
    assert api_docs.lookup("config.does_not_exist_at_all") is None


def test_hover_markdown_renders_signature_and_link() -> None:
    body = api_docs.hover_markdown("kerning")
    assert body.startswith("**kerning** *float*")
    assert "Ren'Py Docs" in body

    body = api_docs.hover_markdown("config.screen_width")
    assert body.startswith("**config.screen_width** = 800")


def test_docstring_entry_parses_args_and_description() -> None:
    import ast as _pyast

    fetch = load_fetch_api()
    src = (
        "def pause(delay=None, music=None, hard=False):\n"
        '    """\n'
        "    :doc: se_pause\n"
        "    :args: (delay=None, *, hard=False)\n"
        "\n"
        "    Causes Ren'Py to pause. Returns true if the user\n"
        "    clicked to end the pause.\n"
        "\n"
        "    `delay`\n"
        "        If given, the number of seconds to pause for.\n"
        '    """\n'
    )
    node = _pyast.parse(src).body[0]
    built = fetch._docstring_entry(_pyast.get_docstring(node), node, "renpy.")
    assert built is not None
    name, entry = built
    assert name == "renpy.pause"
    assert entry["signature"] == "(delay=None, *, hard=False)"
    assert "Causes Ren'Py to pause" in entry["desc"]
    # The parameter table section must not leak into the description.
    assert "number of seconds" not in entry["desc"]


def test_docstring_entry_falls_back_to_code_signature() -> None:
    import ast as _pyast

    fetch = load_fetch_api()
    src = (
        "def notify(message):\n"
        '    """\n'
        "    Causes Ren'Py to display the `message`.\n"
        '    """\n'
    )
    node = _pyast.parse(src).body[0]
    built = fetch._docstring_entry(_pyast.get_docstring(node), node, "renpy.")
    assert built is not None
    assert built[1]["signature"] == "(message)"


def test_parse_export_modules_adds_renpy_functions(tmp_path) -> None:
    fetch = load_fetch_api()
    exports = tmp_path / "py" / "exports"
    exports.mkdir(parents=True)
    (exports / "statementexports.py").write_text(
        "def pause(delay=None):\n"
        '    """\n'
        "    :args: (delay=None)\n"
        "\n"
        "    Pauses the game.\n"
        '    """\n',
        encoding="utf-8",
    )
    entries: dict = {}
    added = fetch.parse_export_modules(entries, tmp_path)
    assert added == 1
    assert entries["renpy.pause"]["desc"] == "Pauses the game."
