# AGENTS.md

本文件约束 AI agent 与贡献者在本仓库的开发、提交、发布流程。历史惯例以此为准的来源：`git log`、`CHANGELOG.md`、`.github/workflows/`。

## 仓库概览

| 路径 | 内容 | 语言 |
| --- | --- | --- |
| `bundled/tools/*.py` | LSP 服务器主体（`lsp_server.py` 入口、`ast_parser.py` 缩进解析器等） | Python 3.11 |
| `src/extension.ts` | VS Code 客户端入口 | TypeScript（webpack 打包） |
| `syntaxes/` | TextMate 语法高亮 | JSON |
| `tests/` | pytest 测试套件 | Python |
| `tools/release.py` | 发布流程工具：版本号、CHANGELOG 骨架、双语 README 覆盖检查、打 tag | Python |
| `scripts/fetch_api.py` | 重新生成 `bundled/tools/renpy_api_docs.json` | Python |

目录约定：`tools/` 放**开发/发布流程自动化**脚本，`scripts/` 放**数据生成**脚本。

Python 侧改动不需要编译；TS 侧改动需要 `pnpm run package` 才能在 VS Code 里生效。

## 环境与常用命令

```bash
pip install -r requirements.txt pytest   # 或 uv sync --extra test
python -m pytest tests/ -q               # 全量测试（CI 同款）
python -m pytest tests/ -q -m "not perf" # 本地快速迭代（跳过 perf 标记）
pnpm install && pnpm run package         # TS 构建验证
```

- 本地 `uv sync` / `pip install` 产生的 `uv.lock` 变更**不要**混进业务提交。
- `.zcode/`、日志文件等本地产物不入库。

## 变更标准流程

每次用户可见的变更，**一个提交带全所有东西**：

1. **代码 + 回归测试**：fix 必须带复现该 bug 的测试；feat 必须带覆盖测试。推送前全量测试通过。
2. **版本号 + CHANGELOG 骨架**（一条命令生成）：

   ```bash
   python tools/release.py bump patch|minor|major [--section Added|Fixed|Changed]
   ```

   递增 `package.json` + `pyproject.toml`，并在 CHANGELOG.md 顶部插入当日段落骨架。版本定级见下节；纯内部重构、CI 调整不 bump、不进 CHANGELOG。
3. **写完 CHANGELOG 段落**（格式见下节），**同步 README.md 与 README.zh-CN.md**：两份**必须同步改**，中文版不是二等公民。
   - 新命令 → `## Commands` / `## 命令`
   - 新设置 → `## Settings` / `## 设置`（含默认值与作用说明）
   - 值得用户感知的功能 → `## Features` / `## 功能特性` 补一句
   - 新增顶层模块 → `### Project Structure` / `### 项目结构`
4. **提交前自查**（`contributes` 里的命令/设置是否都进了两份 README、版本是否一致）：

   ```bash
   python tools/release.py check
   ```

5. **提交并推送** `master`。
6. **发布**（可选，想上架 Marketplace 时）：

   ```bash
   python tools/release.py tag --push   # check 全绿 + 工作区干净才允许打 tag
   ```

   release.yml 会跑测试、校验 tag 与 `package.json` 一致、构建 `.vsix`、发 Marketplace（需 `VSCE_PAT`）和 GitHub Release（Release notes 自动取 CHANGELOG 对应段落）。

> 历史上是「变更提交 + `chore(release)` 提交」两步走；改为单提交后 `chore(release)` 不再需要。若一次发布包含多个变更，各提交各自带版本号递增，最后只需打一次 tag。

## 语义化版本：patch / minor / major

判定问句：**用户能不能说出"它多了一件新事"？**

| 级别 | 判定 | 示例（均来自本仓库历史） |
| --- | --- | --- |
| **minor** `x.y.0` | 新的用户可见**能力**：新命令、新设置、新 LSP 功能面（hover/重命名/签名提示等）、大行为模式 | 签名提示（1.9.0）、workspace symbols（1.11.0）、blankLines 格式化模式（1.6.0） |
| **patch** `x.y.z` | 让既有行为**变正确 / 变快 / 更稳**；**对齐 Ren'Py 引擎本就支持的行为永远算 patch**；既有功能扩展到新场景 | 解析修复（1.11.3）、`1e-3` 格式化修复（1.11.7）、F2 重命名支持点号 define（1.11.5，重命名能力的扩展）、`play/queue` 文件列表解析（1.11.4，引擎本就支持） |
| **major** `x.0.0` | 破坏兼容：移除设置/命令、抬高 VS Code / Ren'Py 最低版本要求。目前无先例，预期极少 | — |

拿不准时的两个辅助问句：改动是否新增了 `package.json` 里的 `contributes.*`（命令/配置）？是 → minor。改动是否只是让 LSP 认识 Ren'Py 引擎本来就有的语法？是 → patch。

## CHANGELOG 写法

- 段落头：`## [x.y.z] - YYYY-MM-DD`（日期为提交日），分类 `### Added` / `### Fixed` / `### Changed`。
- 语言：英文。风格：**加粗导语 + 详述**，写清楚根因、行为、反例/边界——参照 1.11.3、1.11.4 段落的密度，不写"minor fix"式一句话。
- 示例代码用反引号内联（`` `1e-3` → `1e - 3` ``），多个场景用分号或破折号并列。

## 提交信息规范

Conventional Commits，英文祈使句：

```
fix(formatter): keep scientific notation literals intact
feat(lsp): F2 rename for dotted defines
fix(parser): accept init priority on define/default statements
ci: ...
```

- scope 用改动所在子系统：`formatter` / `parser` / `lsp` / `server` / `completion` / `tools` / `ci` / `docs`。
- 正文（body）写根因与行为变化，不写流水账；fix 必须说明"之前错在哪"。
- 一个提交只做一件事；文档与版本号随所属变更走，不单独开 `docs:` / `chore:` 提交（CI、构建链调整除外）。

## 测试要求

- 修复类：先加能复现 bug 的断言（可先跑红），修复后转绿，测试名描述行为而非实现。
- 功能类：覆盖主路径 + 至少一个边界（字符串/注释不受影响、空输入、跨行）。
- 性能敏感路径（索引、解析）改动时跑全量含 perf 标记的套件。

## 发布清单（tag 前）

`python tools/release.py tag` 会强制执行其中可机器判定的项；人工确认的项如下：

- [ ] `python tools/release.py check` 全绿（版本一致、CHANGELOG 段落、双语 README 覆盖）
- [ ] 全量测试通过（含 perf：`python -m pytest tests/ -q`）
- [ ] CHANGELOG 段落已按仓库风格写完（骨架里的 TODO 已替换）
- [ ] `pnpm run package` 构建通过（改过 TS 时）
