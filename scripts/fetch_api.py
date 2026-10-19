#!/usr/bin/env python3
"""Generate ``bundled/tools/renpy_api_docs.json`` from the Ren'Py docs.

Downloads the Sphinx sources of the official documentation, extracts every
``.. function::``, ``.. var::``, ``.. style-property::``,
``.. transform-property::`` and ``.. class::`` entry with its first
description paragraph, and writes them into a single JSON file the
language server loads for hover documentation.

Usage::

    python scripts/fetch_api.py                # ref=master, default output
    python scripts/fetch_api.py --ref 8.3.7   # pin a released version
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

REPO = "renpy/renpy"
SOURCE_DIR = "sphinx/source"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "bundled" / "tools" / "renpy_api_docs.json"

# Source modules whose public functions are re-exported as ``renpy.*``.
# The docs render these from docstrings, so they are absent from the RST
# files; ``renpy.music.*`` lives in the audio module.
EXPORT_MODULES = (
    "actionexports", "clipboardexports", "commonexports", "contextexports",
    "debugexports", "displayexports", "fetchexports", "inputexports",
    "loaderexports", "mediaexports", "menuexports", "persistentexports",
    "platformexports", "predictexports", "restartexports", "rollbackexports",
    "sayexports", "scriptexports", "statementexports",
)
EXPORT_PATHS = tuple(
    (f"renpy/exports/{name}.py", f"exports/{name}.py") for name in EXPORT_MODULES
) + (("renpy/audio/music.py", "exports/music.py"),)

_DIRECTIVE_RE = re.compile(r"^\.\. (function|var|style-property|transform-property|class)::\s*(.*)$")
_CODE_RE = re.compile(r"``([^`]+)``")
_ROLE_RE = re.compile(r':[a-z-]+:`([^`]+)`')
_REF_RE = re.compile(r":ref:`([^`<]+)(?:<[^>]+>)?`")
_EMPH_RE = re.compile(r"\*([^*]+)\*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _github(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "renpy-lsp-fetch-api"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def list_doc_files(ref: str) -> list[str]:
    """All ``*.rst`` file names in the docs source directory."""
    names: list[str] = []
    url = f"https://api.github.com/repos/{REPO}/contents/{SOURCE_DIR}?ref={ref}"
    for entry in json.loads(_github(url)):
        if entry.get("name", "").endswith(".rst"):
            names.append(entry["name"])
    return sorted(names)


def download_files(ref: str, names: list[str], cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = cache / name
        if target.exists():
            continue
        url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{SOURCE_DIR}/{name}"
        target.write_text(_github(url), encoding="utf-8")
        print(f"  fetched {name}")


def _clean(text: str) -> str:
    text = _CODE_RE.sub(r"`\1`", text)
    text = _ROLE_RE.sub(r"`\1`", text)
    text = _REF_RE.sub(r"\1", text)
    text = _EMPH_RE.sub(r"\1", text)
    text = _BACKTICK_RE.sub(r"`\1`", text)
    return re.sub(r"\s+", " ", text).strip()


def _docstring_entry(
    doc: str, node, prefix: str
) -> Optional[Tuple[str, dict]]:
    """Build an entry from a function's docstring.

    ``:args:`` carries the canonical signature when present; the
    description is the first paragraph after the ``:directive:`` meta
    lines.  ``None`` when there is no usable description.
    """
    lines = doc.strip().splitlines() if doc else []
    signature = ""
    body: list = []
    seen_meta = False
    for line in lines:
        m = re.match(r"^:([a-z-]+):\s*(.*)$", line.strip())
        if m:
            seen_meta = True
            if m.group(1) == "args":
                signature = m.group(2).strip()
            continue
        if seen_meta or not body:
            if not line.strip() and not body:
                continue
            if not line.strip():
                break
            body.append(line.strip())
        else:
            break

    if not body:
        return None

    if not signature:
        signature = "(" + _format_call_args(node) + ")"
    elif not signature.startswith("("):
        signature = f"({signature})"

    name = prefix + node.name
    return name, {
        "kind": "function",
        "signature": signature,
        "desc": _clean(" ".join(body)),
        "file": "",
    }


def _format_call_args(node) -> str:
    """Render a function's parameter list from its AST arguments."""
    import ast as _pyast

    a = node.args
    parts: list = []
    defaults = [None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
    for arg, default in zip(a.args, defaults):
        if default is not None:
            parts.append(f"{arg.arg}={_pyast.unparse(default)}")
        else:
            parts.append(arg.arg)
    if a.vararg:
        parts.append(f"*{a.vararg.arg}")
    kw_defaults = [None] * (len(a.kwonlyargs) - len(a.kw_defaults)) + a.kw_defaults
    for arg, default in zip(a.kwonlyargs, kw_defaults):
        if default is not None:
            parts.append(f"{arg.arg}={_pyast.unparse(default)}")
        else:
            parts.append(arg.arg)
    if a.kwarg:
        parts.append(f"**{a.kwarg.arg}")
    return ", ".join(parts)


def parse_export_modules(entries: dict, cache: Path) -> int:
    """Add ``renpy.*`` functions parsed from the source docstrings.

    Docstring entries never shadow an RST entry that already has a
    description, but they do fill undocumented gaps.
    """
    import ast as _pyast

    added = 0
    for rel, cached in EXPORT_PATHS:
        path = cache / "py" / cached
        if not path.exists():
            continue
        try:
            tree = _pyast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            print(f"  skipped {rel}: {exc}")
            continue
        prefix = "renpy.music." if cached == "exports/music.py" else "renpy."
        for node in tree.body:
            if not isinstance(node, _pyast.FunctionDef) or node.name.startswith("_"):
                continue
            if any(isinstance(d, _pyast.Attribute) or d.id == "deprecated"
                   for d in node.decorator_list if isinstance(d, _pyast.Name)):
                continue
            built = _docstring_entry(
                _pyast.get_docstring(node) or "", node, prefix
            )
            if built is None:
                continue
            name, entry = built
            if name not in entries or (not entries[name]["desc"] and entry["desc"]):
                if name not in entries:
                    added += 1
                entries[name] = entry
    return added


def parse_file(name: str, text: str, entries: dict) -> None:
    """Extract directive entries from one RST file into *entries*.

    First writer wins so the alphabetical file order yields stable output;
    an entry with an empty description never shadows a documented one.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _DIRECTIVE_RE.match(lines[i])
        if m is None:
            i += 1
            continue

        kind, arg = m.group(1), m.group(2).strip()
        # Multi-line signatures: continuation lines of a function signature.
        while (
            i + 1 < len(lines)
            and lines[i + 1].startswith((" " * 4, "\t"))
            and lines[i + 1].strip()
            and not _DIRECTIVE_RE.match(lines[i + 1])
            and re.search(r"[,\\(]$", arg)
        ):
            i += 1
            arg += " " + lines[i].strip()

        # Description: following indented non-blank lines (first paragraph).
        desc_lines: list[str] = []
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                if desc_lines:
                    break
                j += 1
                continue
            if not line.startswith((" ", "\t")) or line.lstrip().startswith(".. "):
                break
            desc_lines.append(line.strip())
            j += 1

        if kind == "var":
            # "config.name = \"\"" / "gui.text_size = 22"
            parts = arg.split("=", 1) if "=" in arg else (arg, "")
            doc_name = parts[0].strip()
            signature = ("= " + parts[1].strip()) if len(parts) > 1 and parts[1].strip() else ""
        elif kind in ("function", "class"):
            doc_name = re.split(r"[(\s:]", arg, maxsplit=1)[0]
            rest = arg[len(doc_name):].strip()
            signature = rest if rest.startswith("(") else f"({rest})" if rest else ""
        else:  # style-property / transform-property: "bold boolean"
            parts = arg.split(None, 1)
            doc_name = parts[0] if parts else ""
            signature = parts[1].strip() if len(parts) > 1 else ""

        desc = _clean(" ".join(desc_lines))
        if doc_name and (doc_name not in entries or (not entries[doc_name]["desc"] and desc)):
            entries[doc_name] = {
                "kind": kind,
                "signature": signature,
                "desc": desc,
                "file": name[: -len(".rst")],
            }
        i = j


def download_export_modules(ref: str, cache: Path) -> None:
    for rel, cached in EXPORT_PATHS:
        target = cache / "py" / cached
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{rel}"
        target.write_text(_github(url), encoding="utf-8")
        print(f"  fetched {rel}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="master", help="renpy/renpy git ref (default: master)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output JSON path")
    ap.add_argument("--cache", type=Path, default=Path(".renpy-docs-cache"), help="RST download cache")
    ap.add_argument(
        "--offline", action="store_true",
        help="parse the cache directory only (no listing/download)",
    )
    args = ap.parse_args()

    if args.offline:
        names = sorted(p.name for p in args.cache.glob("*.rst"))
        print(f"Offline: {len(names)} cached RST files")
    else:
        print(f"Listing docs at ref {args.ref}…")
        names = list_doc_files(args.ref)
        print(f"{len(names)} RST files")
        download_files(args.ref, names, args.cache)
        print("Fetching export sources…")
        download_export_modules(args.ref, args.cache)

    entries: dict = {}
    for name in names:
        parse_file(name, (args.cache / name).read_text(encoding="utf-8"), entries)
    docstring_count = parse_export_modules(entries, args.cache)
    print(f"  +{docstring_count} renpy.* functions from source docstrings")

    payload = {
        "renpy_ref": args.ref,
        "generated": datetime.date.today().isoformat(),
        "count": len(entries),
        "entries": entries,
    }
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(f"{len(entries)} entries → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
