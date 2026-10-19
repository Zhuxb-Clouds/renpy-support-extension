# Ren'Py 语言支持

[English](README.md)

一个为 Ren'Py 脚本文件（`.rpy`、`.rpym`）提供语言支持的 Visual Studio Code 扩展。

## 功能特性

- **语法高亮** — 完整的 Ren'Py 脚本语法高亮，包括 screen、style、ATL 和嵌入式 Python
- **代码格式化** — 通过内置 LSP 服务器自动缩进和格式化
- **上下文补全** — label、image、transform、transition、screen、style、`define`/`default` 变量（含 `music.` 这类点分命名空间）、screen/ATL/style 属性，以及**显示构件构造函数的关键字参数**（`Transform(` → `xalign=…`、`Text(` → `kerning=…`），支持跨行嵌套调用，并自动过滤已写过的参数
- **签名帮助** — `call label(...)` / `call screen ...(...)` 显示真实参数；语句模板展示可用的子句关键字（`at`、`with`、`fadeout` 等）；显示构件构造函数（`Transform(`、`Text(`、`Fixed(` …）显示关键字参数签名，并按名称精确高亮光标所在的实参
- **诊断功能** — 常见问题的警告和错误提示：
  - 未定义的 `jump`/`call` label、重复的 label/screen 定义、缺失的图像文件、未使用的 label
  - `show`/`scene`/`hide` 引用未定义的 image、`at` 引用未知的 transform、`style x is parent` 的父样式不存在
  - 未闭合的字符串字面量，作为错误报告在引号所在行
- **引擎 API 悬停文档** — 1070+ 条来自官方文档与引擎源码 docstring 的条目（`config.*`、`gui.*`、`renpy.*` 函数、style/transform 属性、displayable 类）；悬停 `kerning`、`config.screen_width` 或 `renpy.pause` 即可查看官方说明
- **媒体预览** — 悬停 `show`/`scene`/`image` 行直接显示图片；`play`/`queue`/`voice` 行给出链接，可在预览面板中播放音频/视频
- **工作区符号** — `Ctrl+T` 在全部 label、screen、transform、image、style、define、default 中模糊搜索
- **调用层次** — `Shift+Alt+H` 查看 label（或 `jump`/`call` 位置）的调用方与跳转目标
- **快速修复** — 针对诊断的 Code Action：一键创建缺失的 label、删除未使用的 label 块
- **翻译工具** — `tl/` 目录的过期翻译诊断、对话行与翻译条目之间的跳转、按语言的翻译覆盖率报告
- **翻译 ID 悬停** — 悬停对话行可查看 Ren'Py 翻译标识符
- **Markdown 支持** — Markdown 文件中 Ren'Py 代码块的语法高亮

## 安装

从 [VS Code 扩展商店](https://marketplace.visualstudio.com/items?itemName=zhuxb-clouds.renpy-support-extension) 安装，或在 VS Code 扩展中搜索 "Ren'Py Language Support"。

## 环境要求

- VS Code 1.74.0 或更高版本
- Python 3.11+（用于语言服务器）

扩展会自动从 `.venv/bin/python3` 或系统 `python3` 检测 Python。你也可以在设置中配置自定义路径。

## 命令

打开命令面板（`Ctrl+Shift+P` / `Cmd+Shift+P`）并输入：

| 命令                                  | 描述                           |
| ------------------------------------- | ------------------------------ |
| `Ren'Py LSP: Start Language Server`   | 启动语言服务器                 |
| `Ren'Py LSP: Stop Language Server`    | 停止语言服务器                 |
| `Ren'Py LSP: Restart Language Server` | 重启语言服务器                 |
| `Ren'Py LSP: Format All Ren'Py Files` | 格式化工作区中所有 `.rpy` 文件 |
| `Ren'Py LSP: Show Translation Report` | 按语言显示翻译覆盖率 |
| `Ren'Py LSP: Run Ren'Py Lint` | 运行 Ren'Py SDK 的 lint（需设置 `renpy-lsp.sdkPath`） |
| `Ren'Py LSP: Launch Project` | 通过 SDK 启动游戏 |

## 设置

| 设置项                            | 默认值 | 描述                                       |
| --------------------------------- | ------ | ------------------------------------------ |
| `renpy-lsp.pythonPath`            | `""`   | 自定义 Python 解释器路径（留空则自动检测） |
| `renpy-lsp.sdkPath`               | `""`   | Ren'Py SDK 目录（启用 lint 与启动游戏命令） |
| `renpy-lsp.formatting.enabled`    | `true` | 启用文档格式化                             |
| `renpy-lsp.formatting.indentSize` | `4`    | 每级缩进的空格数                           |
| `renpy-lsp.formatting.blankLines` | `collapse` | 空行处理：`preserve`（不动）、`collapse`（折叠为一行）、`betweenSay`（另在 `label` 脚本块内的对白/旁白行之间插入空行）、`strip`（全部删除） |
| `renpy-lsp.diagnostics.enabled`   | `true` | 启用诊断功能                               |
| `renpy-lsp.diagnostics.fullOnSave` | `false` | 保存时也重跑完整诊断(编辑时已随变更队列重跑;内容未变化的保存不会重跑) |
| `renpy-lsp.diagnostics.severity`  | `{}` | 按检查项覆盖严重级别,如 `{ "unused-label": "none", "undefined-transform": "information" }`;`none` 表示彻底关闭该检查 |
| `renpy-lsp.previews.enabled` | `true` | 悬停媒体预览:`show`/`scene`/`image` 行显示图片,`play`/`queue`/`voice` 行显示音频/视频预览链接 |
| `renpy-lsp.previews.maxSize` | `400` | 悬停图片预览的最大宽高(像素);超出的图片等比缩小到框内,小图不放大 |
| `renpy-lsp.logLevel`              | `info` | 输出面板中语言服务器日志的详细程度（`error`/`warning`/`info`/`debug`） |

### 项目级格式化配置

风格设置（`indentSize`、`blankLines`）可通过工作区根目录的 `.renpy-format.json` 文件随项目提交：

```json
{
  "indentSize": 2,
  "blankLines": "betweenSay"
}
```

该文件存在时，按键覆盖对应的 VS Code 设置。

## 开发

### 环境配置

```bash
# 克隆仓库
git clone https://github.com/Zhuxb-Clouds/renpy-support-extension.git
cd renpy-support-extension

# 安装 Node.js 依赖
npm install

# 创建 Python 虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 构建

```bash
npm run compile      # 开发构建
npm run package      # 生产构建
npm run vsix         # 打包 .vsix 文件
```

### 发布

发布由 GitHub Actions 自动完成（`.github/workflows/release.yml`）：

1. 确认 `CHANGELOG.md` 已包含新版本的段落，且 `package.json` / `pyproject.toml` 已更新到该版本。
2. 创建并推送与版本号一致的 tag：

   ```bash
   git tag v1.7.0
   git push origin v1.7.0
   ```

3. workflow 会运行测试、校验 tag 与 `package.json` 版本一致、构建 `.vsix`、发布到 VS Code 扩展商店（需要在仓库配置 `VSCE_PAT` secret，未配置时跳过该步），并创建附有 `.vsix` 和 CHANGELOG 摘要的 GitHub Release。

push 和 PR 会由 `.github/workflows/ci.yml` 检查（Python 测试 + 生产构建）。

### 项目结构

- `src/extension.ts` — VS Code 客户端入口
- `bundled/tools/lsp_server.py` — Python LSP 服务器（使用 pygls）
- `bundled/tools/ast_parser.py` — 基于缩进的 Ren'Py 解析器
- `bundled/tools/renpy_api_docs.json` — 引擎 API 悬停文档，由 `scripts/fetch_api.py` 生成（`python scripts/fetch_api.py --ref <renpy tag>` 重新生成；`--offline` 只解析本地缓存）
- `syntaxes/` — TextMate 语法高亮文件

## 许可证

ISC 许可证。详见 [LICENSE](LICENSE)。

## 贡献

欢迎在 [GitHub](https://github.com/Zhuxb-Clouds/renpy-support-extension) 提交 Issue 和 Pull Request。
