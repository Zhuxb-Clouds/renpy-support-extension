#!/usr/bin/env python3
"""发布流程工具：版本号、CHANGELOG、双语 README 一致性、打 tag。

AGENTS.md 中「变更标准流程」与「发布清单」的可执行版本。仅用标准库。

用法：
  python tools/release.py bump patch|minor|major [--section Added|Fixed|Changed] [--dry-run]
      递增 package.json + pyproject.toml 版本号，并在 CHANGELOG.md 顶部插入新版本段落骨架。
      产物应随本次变更一起提交（单提交流）。

  python tools/release.py check
      校验发布清单中可机器判定的项：版本号一致、CHANGELOG 段落、
      contributes 里的命令/设置在两份 README 中均有记录。退出码非 0 即有失败项。

  python tools/release.py tag [--push] [--dry-run]
      check 全绿 + 工作区干净后，创建 vX.Y.Z tag（--push 时推送 tag）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
READMES = ((ROOT / "README.md", ("Commands", "Settings")), (ROOT / "README.zh-CN.md", ("命令", "设置")))

CHANGELOG_TODO = "- **TODO：<加粗导语>。** <根因、行为、边界/反例——参照 1.11.3 / 1.11.4 段落的密度，最终成文用英文。>"


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def read_version() -> str:
    pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    version = pkg.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"package.json 的 version 不是 x.y.z 格式：{version!r}")
    return version


def bump_version(version: str, level: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_version(new_version: str) -> None:
    pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    pkg["version"] = new_version
    PACKAGE_JSON.write_text(json.dumps(pkg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    updated, n = re.subn(r'(?m)^version = "[^"]+"', f'version = "{new_version}"', pyproject, count=1)
    if n != 1:
        sys.exit("pyproject.toml 中找不到顶层 version 行")
    PYPROJECT.write_text(updated, encoding="utf-8")


def scaffold_changelog(new_version: str, section: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if re.search(rf"(?m)^## \[{re.escape(new_version)}\]", text):
        sys.exit(f"CHANGELOG.md 已有 {new_version} 段落；如需重建请先手工删除")
    entry = f"## [{new_version}] - {date.today().isoformat()}\n\n### {section}\n\n{CHANGELOG_TODO}\n\n"
    updated, n = re.subn(r"(?m)^(# Changelog\n\n)", r"\1" + entry, text, count=1)
    if n != 1:
        sys.exit("CHANGELOG.md 顶部缺少 '# Changelog' 头")
    CHANGELOG.write_text(updated, encoding="utf-8")


def read_changelog_section(version: str) -> tuple[bool, str]:
    """返回 (存在与否, 诊断信息)，并校验日期格式。"""
    m = re.search(rf"(?ms)^## \[{re.escape(version)}\] - (\S+)\n", CHANGELOG.read_text(encoding="utf-8"))
    if m is None:
        return False, f"CHANGELOG.md 顶部没有 {version} 的段落"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group(1)):
        return False, f"{version} 段落日期格式不是 YYYY-MM-DD：{m.group(1)}"
    return True, m.group(1)


def extract_section(readme: Path, heading: str) -> str:
    """取 markdown 二级标题段的正文，找不到返回空串。"""
    lines: list[str] = []
    in_section = False
    for line in readme.read_text(encoding="utf-8").splitlines():
        if re.fullmatch(r"## .+", line):
            if in_section:
                break
            in_section = line[3:].strip() == heading
            continue
        if in_section:
            lines.append(line)
    return "\n".join(lines)


def check_readme_coverage(pkg: dict) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    commands = pkg.get("contributes", {}).get("commands", [])
    properties = pkg.get("contributes", {}).get("configuration", {}).get("properties", {})
    for readme, (commands_heading, settings_heading) in READMES:
        label = readme.name
        commands_md = extract_section(readme, commands_heading)
        settings_md = extract_section(readme, settings_heading)
        for cmd in commands:
            title = cmd.get("title", cmd["command"])
            # README 里命令以命令面板形式出现（`Ren'Py LSP: <title>`），做子串匹配
            if title not in commands_md:
                results.append((False, f"{label} {commands_heading} 段缺少命令 `{title}`"))
        for key in properties:
            if f"`{key}`" not in settings_md:
                results.append((False, f"{label} {settings_heading} 段缺少设置 `{key}`"))
        if not any(r[0] is False and r[1].startswith(label) for r in results):
            results.append(
                (True, f"{label}：{len(commands)} 个命令、{len(properties)} 个设置全部有记录")
            )
    return results


def run_checks(clean_tree_required: bool) -> bool:
    pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    version = pkg["version"]
    ok = True

    print(f"package.json 版本：{version}")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version = "([^"]+)"', pyproject)
    if m and m.group(1) == version:
        _ok(f"pyproject.toml 版本一致（{version}）")
    else:
        _fail(f"pyproject.toml 版本（{m.group(1) if m else '缺失'}）≠ package.json（{version}）")
        ok = False

    found, detail = read_changelog_section(version)
    if found:
        _ok(f"CHANGELOG.md 有 {version} 段落，日期 {detail}")
    else:
        _fail(detail)
        ok = False

    for passed, msg in check_readme_coverage(pkg):
        (_ok if passed else _fail)(msg)
        ok = ok and passed

    if clean_tree_required:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True)
        if dirty.stdout.strip():
            _fail("工作区不干净，先提交全部变更再打 tag")
            ok = False
        else:
            _ok("工作区干净")

    return ok


def cmd_bump(args: argparse.Namespace) -> None:
    old = read_version()
    new = bump_version(old, args.level)
    print(f"{old} → {new}（{args.level}，CHANGELOG 分类：{args.section}）")
    if args.dry_run:
        print("（dry-run，未写入）")
        return
    write_version(new)
    scaffold_changelog(new, args.section)
    print("已写入 package.json、pyproject.toml、CHANGELOG.md。")
    print("记得：CHANGELOG 段落按仓库风格写完（英文），README 双语如有涉及需同步，全部随本次变更一个提交。")


def cmd_check(_args: argparse.Namespace) -> None:
    if not run_checks(clean_tree_required=False):
        sys.exit(1)


def cmd_tag(args: argparse.Namespace) -> None:
    version = read_version()
    tag = f"v{version}"
    if not run_checks(clean_tree_required=True):
        sys.exit("校验未通过，未创建 tag")
    existing = subprocess.run(["git", "tag", "--list", tag], cwd=ROOT, capture_output=True, text=True, check=True)
    if existing.stdout.strip():
        sys.exit(f"tag {tag} 已存在")
    print(f"创建 tag {tag}")
    if args.dry_run:
        print("（dry-run，未执行）")
        return
    subprocess.run(["git", "tag", tag], cwd=ROOT, check=True)
    if args.push:
        subprocess.run(["git", "push", "origin", tag], cwd=ROOT, check=True)
        print(f"已推送 {tag}，release.yml 将自动构建发布。")
    else:
        print(f"已创建 {tag}；推送到 origin 后 release.yml 才会触发。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_bump = sub.add_parser("bump", help="递增版本号并生成 CHANGELOG 骨架")
    p_bump.add_argument("level", choices=("patch", "minor", "major"))
    p_bump.add_argument("--section", choices=("Added", "Fixed", "Changed"), default="Fixed")
    p_bump.add_argument("--dry-run", action="store_true")
    p_bump.set_defaults(func=cmd_bump)

    p_check = sub.add_parser("check", help="校验发布清单中可机器判定的项")
    p_check.set_defaults(func=cmd_check)

    p_tag = sub.add_parser("tag", help="校验通过后创建（可选推送）发布 tag")
    p_tag.add_argument("--push", action="store_true")
    p_tag.add_argument("--dry-run", action="store_true")
    p_tag.set_defaults(func=cmd_tag)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
