"""Performance budget & scaling tests for the Ren'Py LSP server.

These guard the hot paths against regressions (especially accidental
quadratic behavior) rather than measure absolute speed, so budgets are
deliberately generous (~5-10× typical hardware timings).

Run everything (timings are printed):

    python -m pytest tests/test_performance.py -q -s

Skip them during quick iteration:

    python -m pytest tests/ -q -m "not perf"

Synthetic projects are generated in ``tmp_path``: N script files with
labels, dialogue, defines, images, transforms, screens, and cross-file
jumps, plus one oversized single file for parser/formatter throughput.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

import server_context as ctx
from ast_parser import RpyParser
from workspace_index import WorkspaceIndex

import diagnostics

pytestmark = pytest.mark.perf

URI_ROOT = "file:///perf-project"


# ── synthetic project builders ───────────────────────────────────────────


def _script_text(index: int, labels: int, says: int) -> str:
    lines: list[str] = [f"# generated script {index}", "init offset = 0"]
    for c in range(3):
        lines.append(f'define char_{index}_{c} = Character("C{index}{c}")')
    for c in range(4):
        lines.append(f'image bg_{index}_{c} = "images/bg_{index}_{c}.png"')
    for c in range(3):
        lines.append(f"transform tr_{index}_{c}:")
        lines.append("    xalign 0.5")
    lines.append(f"screen scr_{index}:")
    lines.append('    text "hello"')
    for l in range(labels):
        lines.append(f"label label_{index}_{l}:")
        for s in range(says):
            lines.append(f'    char_{index}_0 "dialogue {index} {l} {s}"')
        lines.append(f"    show bg_{index}_0 at tr_{index}_0 with dissolve")
        target = (
            f"label_{index}_{l + 1}"
            if l + 1 < labels
            else f"label_{(index + 1) % 1000}_0"  # cross-file jump
        )
        lines.append(f"    jump {target}")
    return "\n".join(lines) + "\n"


def build_project(root: Path, *, n_files: int, labels: int, says: int) -> Path:
    game = root / "game"
    images = game / "images"
    images.mkdir(parents=True)
    for i in range(n_files):
        (game / f"script_{i}.rpy").write_text(
            _script_text(i, labels, says), encoding="utf-8"
        )
        for c in range(4):
            # Placeholder files so image references resolve without globbing.
            (images / f"bg_{i}_{c}.png").write_bytes(b"")
    return root


def build_big_file(root: Path, *, labels: int, says: int) -> tuple[Path, str]:
    """One oversized file: `labels` labels of dialogue/show/jump each."""
    path = root / "game" / "big.rpy"
    text = _script_text(9000, labels, says)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text


# ── measurement helpers ──────────────────────────────────────────────────


def best_of(fn, repeats: int = 2) -> float:
    """Best (min) wall-clock seconds over *repeats* runs — reduces noise."""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def uri_from_path(path) -> str:
    return Path(path).resolve().as_uri()


def clear_server_caches() -> None:
    """Reset the shared parse/index caches so measurements start cold."""
    with ctx._cache_lock:
        ctx._parse_cache.clear()
        ctx._path_to_uri.clear()
    ctx._renpy_py_cache.clear()
    ctx._image_cache.clear()


def make_index(root: Path) -> WorkspaceIndex:
    """A WorkspaceIndex over *root* backed by the server's real helpers."""
    server = SimpleNamespace(
        workspace=SimpleNamespace(
            folders={"root": SimpleNamespace(uri=uri_from_path(root))}
        )
    )
    return WorkspaceIndex(
        server=server,
        parse_cache=ctx._parse_cache,
        cache_lock=ctx._cache_lock,
        path_to_uri=ctx._path_to_uri,
        path_from_uri_fn=ctx._path_from_uri,
        normalize_path_fn=ctx._normalize_path_key,
        get_parse_for_file_fn=ctx._get_parse_for_file,
    )


def path_from_uri(uri: str) -> str:
    return unquote(urlparse(uri).path)


# ── parser throughput ────────────────────────────────────────────────────


def test_parse_large_file_budget(tmp_path: Path) -> None:
    _path, text = build_big_file(tmp_path, labels=600, says=12)
    n_lines = text.count("\n")
    elapsed = best_of(lambda: RpyParser(text).parse())
    print(f"\nparse {n_lines} lines ({len(text)} chars): {elapsed * 1000:.0f} ms")
    assert elapsed < 1.5, f"parsing {n_lines} lines took {elapsed:.2f}s"


def test_parse_scales_linearly(tmp_path: Path) -> None:
    _p1, small = build_big_file(tmp_path / "a", labels=300, says=12)
    _p2, big = build_big_file(tmp_path / "b", labels=600, says=12)
    t_small = best_of(lambda: RpyParser(small).parse())
    t_big = best_of(lambda: RpyParser(big).parse())
    print(
        f"\nparse scaling: {len(small)} chars → {t_small * 1000:.0f} ms, "
        f"{len(big)} chars (2×) → {t_big * 1000:.0f} ms"
    )
    # Guard against quadratic behavior: doubling input must not double² time.
    assert t_big < t_small * 3.0 + 0.05


# ── workspace index warm-up ──────────────────────────────────────────────


def test_index_warmup_budget(tmp_path: Path, monkeypatch) -> None:
    build_project(tmp_path, n_files=100, labels=12, says=6)
    clear_server_caches()
    monkeypatch.setattr(ctx, "_image_cache_built", False)
    index = make_index(tmp_path)

    t0 = time.perf_counter()
    index.ensure_current_parallel()
    elapsed = time.perf_counter() - t0

    n_files = len(index.get_file_list())
    print(f"\nindex warm-up ({n_files} files): {elapsed * 1000:.0f} ms")
    assert n_files == 100
    assert elapsed < 5.0, f"warming up {n_files} files took {elapsed:.2f}s"


# ── full diagnostics (cross-workspace checks) ────────────────────────────


def test_full_diagnostics_cold_then_warm(tmp_path: Path, monkeypatch) -> None:
    build_project(tmp_path, n_files=100, labels=12, says=6)
    clear_server_caches()
    monkeypatch.setattr(ctx, "_image_cache_built", False)
    index = make_index(tmp_path)
    monkeypatch.setattr(ctx, "_workspace_index", index)
    # The real search-dir/renpy-file lookups read LSP_SERVER.workspace,
    # which is uninitialized in tests; point them at the synthetic project
    # (in production these come from the workspace folders).
    monkeypatch.setattr(
        ctx, "_get_renpy_search_dirs", lambda: [str(tmp_path / "game")]
    )
    monkeypatch.setattr(ctx, "_get_workspace_renpy_py_files", lambda: [])

    first_path = tmp_path / "game" / "script_0.rpy"
    # Parse from disk (the background-diagnostics path) — the LSP-facing
    # _get_parse would read an empty document since nothing is "open".
    uri, _ast, parser = ctx._get_parse_for_file(str(first_path))

    # Cold: first run also triggers ensure_current() → parses all 100 files.
    t0 = time.perf_counter()
    cold = diagnostics._collect_full_diagnostics(uri, parser)
    cold_elapsed = time.perf_counter() - t0

    # Warm: index and parse caches are hot — the per-save cost.
    warm_elapsed = best_of(
        lambda: diagnostics._collect_full_diagnostics(uri, parser), repeats=3
    )

    print(
        f"\nfull diagnostics (100-file project): cold {cold_elapsed * 1000:.0f} ms, "
        f"warm {warm_elapsed * 1000:.1f} ms, {len(cold)} diagnostic(s)"
    )
    assert cold_elapsed < 5.0, f"cold diagnostics took {cold_elapsed:.2f}s"
    assert warm_elapsed < 0.1, f"warm diagnostics took {warm_elapsed * 1000:.0f} ms"


# ── completion latency ───────────────────────────────────────────────────


def test_completion_latency_budget(tmp_path: Path, monkeypatch) -> None:
    from completion import _completion_items_for_context

    _path, text = build_big_file(tmp_path, labels=400, says=4)
    parser = RpyParser(text)
    parser.parse()
    lines = text.splitlines()

    def symbol_map(getter) -> dict:
        result: dict = {}
        for node in getter(parser):
            result.setdefault(node.name, []).append((uri_from_path(tmp_path), node))
        return result

    for attr, getter in {
        "_get_all_workspace_labels": RpyParser.get_all_labels,
        "_get_all_workspace_defines": RpyParser.get_all_defines,
        "_get_all_workspace_defaults": RpyParser.get_all_defaults,
        "_get_all_workspace_screens": RpyParser.get_all_screens,
        "_get_all_workspace_images": RpyParser.get_all_images,
        "_get_all_workspace_transforms": RpyParser.get_all_transforms,
        "_get_all_workspace_styles": RpyParser.get_all_styles,
    }.items():
        monkeypatch.setattr(ctx, attr, lambda g=getter: symbol_map(g))
    monkeypatch.setattr(ctx, "_ensure_image_cache", lambda: None)
    monkeypatch.setattr(ctx, "_image_cache", {})

    # A jump-context line (label completions) and a bare line (keywords +
    # variables + labels) — the two heaviest contexts.
    jump_line = next(i for i, l in enumerate(lines) if l.strip().startswith("jump "))
    contexts = [
        (lines, jump_line, len(lines[jump_line])),
        (lines + [""], len(lines), 0),
    ]
    best_of(lambda: [_completion_items_for_context("u", parser, *c) for c in contexts])
    elapsed = best_of(
        lambda: [_completion_items_for_context("u", parser, *c) for c in contexts],
        repeats=3,
    )
    print(f"\ncompletion (2 heavy contexts): {elapsed * 1000:.1f} ms")
    assert elapsed < 0.3, f"completion took {elapsed * 1000:.0f} ms"


# ── formatter throughput ─────────────────────────────────────────────────


class _FakeDoc:
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(True)


class _FakeWorkspace:
    def __init__(self, source: str) -> None:
        self._doc = _FakeDoc(source)

    def get_text_document(self, uri: str) -> _FakeDoc:
        return self._doc


class _FakeLS:
    def __init__(self, source: str) -> None:
        self.workspace = _FakeWorkspace(source)


def test_format_large_file_budget(tmp_path: Path, monkeypatch) -> None:
    from lsprotocol import types

    import lsp_server

    _path, text = build_big_file(tmp_path, labels=600, says=12)
    monkeypatch.setitem(ctx._settings["formatting"], "enabled", True)
    monkeypatch.setitem(ctx._settings["formatting"], "indentSize", 4)
    monkeypatch.setitem(ctx._settings["formatting"], "blankLines", "betweenSay")
    params = types.DocumentFormattingParams(
        text_document=types.TextDocumentIdentifier(uri="file:///perf/big.rpy"),
        options=types.FormattingOptions(tab_size=4, insert_spaces=True),
    )
    ls = _FakeLS(text)

    def run():
        edits = lsp_server.format_document(ls, params)
        assert edits, "formatter produced no edits"

    run()  # warm-up (regex caches etc.)
    elapsed = best_of(run)
    print(f"\nformat {text.count(chr(10))} lines: {elapsed * 1000:.0f} ms")
    assert elapsed < 1.5, f"formatting took {elapsed:.2f}s"
