from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import server_context as ctx
from ast_parser import RpyParser

import translation



def write_project(tmp_path: Path, source: str, tl: str) -> tuple[str, str]:
    src = tmp_path / "game" / "script.rpy"
    tlf = tmp_path / "game" / "tl" / "chinese" / "script.rpy"
    src.parent.mkdir(parents=True, exist_ok=True)
    tlf.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(dedent(source).strip("\n") + "\n", encoding="utf-8")
    tlf.write_text(dedent(tl).strip("\n") + "\n", encoding="utf-8")
    return src.as_uri(), tlf.as_uri()


def patch_workspace(monkeypatch, tmp_path: Path) -> tuple[str, str]:
    """Register the synthetic project; returns (source_uri, tl_uri)."""
    files = [
        str(tmp_path / "game" / "script.rpy"),
        str(tmp_path / "game" / "tl" / "chinese" / "script.rpy"),
    ]
    monkeypatch.setattr(ctx, "_get_workspace_rpy_files", lambda: files)
    monkeypatch.setattr(ctx, "_get_renpy_search_dirs", lambda: [str(tmp_path / "game")])
    monkeypatch.setattr(ctx, "_get_workspace_renpy_py_files", lambda: [])

    def labels():
        src = files[0]
        _uri, _ast, parser = ctx._get_parse_for_file(src)
        result: dict = {}
        for node in parser.get_all_labels():
            result.setdefault(node.name, []).append((ctx._uri_from_path(src), node))
        return result

    monkeypatch.setattr(ctx, "_get_all_workspace_labels", labels)
    return ctx._uri_from_path(files[0]), ctx._uri_from_path(files[1])


def parse(source: str) -> RpyParser:
    parser = RpyParser(dedent(source).strip("\n") + "\n")
    parser.parse()
    return parser


def codes(diags) -> list:
    return [str(d.code) for d in diags]


VALID_TL_BLOCK_ID = translation._renpy_translate_id("start", "e", "Hello")


# ── consistency checks ───────────────────────────────────────────────────


def test_valid_translation_produces_no_diagnostics(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        """
        label start:
            e "Hello"
        """,
        f"""
        translate chinese strings:
            old "Hello"
            new "你好"

        translate chinese {VALID_TL_BLOCK_ID}:
            e "你好"
        """,
    )
    src_uri, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse("translate chinese strings:\n    old \"Hello\"\n    new \"你好\"\n")
    diags = []
    translation.check_translations(tl_uri, parser, diags)
    assert diags == []


def test_stale_old_string_is_reported(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        """
        label start:
            e "Hello now"
        """,
        """
        translate chinese strings:
            old "Hello"
            new "你好"
        """,
    )
    _src, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse('translate chinese strings:\n    old "Hello"\n    new "你好"\n')
    diags = []
    translation.check_translations(tl_uri, parser, diags)
    assert codes(diags) == ["translation-stale"]


def test_old_without_new_is_an_error(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        'translate chinese strings:\n    old "Hello"\n',
    )
    _src, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse('translate chinese strings:\n    old "Hello"\n')
    diags = []
    translation.check_translations(tl_uri, parser, diags)
    assert codes(diags) == ["translation-missing-new"]


def test_new_without_old_is_an_error(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        'translate chinese strings:\n    new "你好"\n',
    )
    _src, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse('translate chinese strings:\n    new "你好"\n')
    diags = []
    translation.check_translations(tl_uri, parser, diags)
    assert codes(diags) == ["translation-missing-old"]


def test_stale_dialogue_block_is_reported(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        f"translate chinese {VALID_TL_BLOCK_ID}:\n"
        '    e "你好"\n'
        "\n"
        "translate chinese start_ffffffff:\n"
        '    e "旧的台词"\n',
    )
    _src, tl_uri = patch_workspace(monkeypatch, tmp_path)
    src = (
        f"translate chinese {VALID_TL_BLOCK_ID}:\n"
        '    e "你好"\n'
        "translate chinese start_ffffffff:\n"
        '    e "旧的台词"\n'
    )
    parser = parse(src)
    diags = []
    translation.check_translations(tl_uri, parser, diags)
    assert codes(diags) == ["translation-stale"]


def test_source_files_are_never_checked(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        'translate chinese strings:\n    old "不存在"\n    new "x"\n',
    )
    src_uri, _tl = patch_workspace(monkeypatch, tmp_path)
    parser = parse('label start:\n    e "Hello"\n')
    diags = []
    translation.check_translations(src_uri, parser, diags)
    assert diags == []


# ── navigation ───────────────────────────────────────────────────────────


def test_source_say_jumps_to_tl_entry(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        f'translate chinese {VALID_TL_BLOCK_ID}:\n    e "你好"\n',
    )
    src_uri, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse('label start:\n    e "Hello"\n')
    locs = translation.find_translation_jump(src_uri, 1, parser)  # say line
    assert locs and len(locs) == 1
    assert locs[0].uri == tl_uri
    assert locs[0].range.start.line == 1  # translated say line


def test_source_say_jumps_to_strings_new_line(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        'translate chinese strings:\n    old "Hello"\n    new "你好"\n',
    )
    src_uri, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse('label start:\n    e "Hello"\n')
    locs = translation.find_translation_jump(src_uri, 1, parser)
    assert locs and locs[0].uri == tl_uri
    assert locs[0].range.start.line == 2  # the new "你好" line


def test_tl_new_jumps_back_to_source(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n',
        f'translate chinese {VALID_TL_BLOCK_ID}:\n    e "你好"\n',
    )
    src_uri, tl_uri = patch_workspace(monkeypatch, tmp_path)
    parser = parse(f'translate chinese {VALID_TL_BLOCK_ID}:\n    e "你好"\n')
    locs = translation.find_translation_jump(tl_uri, 1, parser)
    assert locs and locs[0].uri == src_uri
    assert locs[0].range.start.line == 1


# ── coverage report ──────────────────────────────────────────────────────


def test_translation_report_counts(tmp_path, monkeypatch) -> None:
    write_project(
        tmp_path,
        'label start:\n    e "Hello"\n    e "Bye"\n    e "Untranslated"\n',
        f'translate chinese {VALID_TL_BLOCK_ID}:\n'
        '    e "你好"\n'
        "\n"
        "translate chinese start_ffffffff:\n"
        '    e "过期"\n'
        "\n"
        'translate chinese strings:\n'
        '    old "Hello"\n'
        '    new "你好"\n'
        '    old "改掉了"\n'
        '    new "旧的"\n',
    )
    patch_workspace(monkeypatch, tmp_path)
    report = translation.translation_report()

    assert report["sourceDialogue"] == 3
    (zh,) = [e for e in report["languages"] if e["language"] == "chinese"]
    assert zh["translatedDialogue"] == 1
    assert zh["staleDialogue"] == 1
    assert zh["stringsPairs"] == 2
    assert zh["staleStrings"] == 1
