# Ren'Py 语言支持

[English](README.md)

一个为 Ren'Py 脚本文件（`.rpy`、`.rpym`）提供语言支持的 Visual Studio Code 扩展。

## 功能特性

- **语法高亮** — 完整的 Ren'Py 脚本语法高亮，包括 screen、style、ATL 和嵌入式 Python
- **代码格式化** — 通过内置 LSP 服务器自动缩进和格式化
- **诊断功能** — 常见问题的警告和错误提示
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

## 设置

| 设置项                            | 默认值 | 描述                                       |
| --------------------------------- | ------ | ------------------------------------------ |
| `renpy-lsp.pythonPath`            | `""`   | 自定义 Python 解释器路径（留空则自动检测） |
| `renpy-lsp.formatting.enabled`    | `true` | 启用文档格式化                             |
| `renpy-lsp.formatting.indentSize` | `4`    | 每级缩进的空格数                           |
| `renpy-lsp.diagnostics.enabled`   | `true` | 启用诊断功能                               |

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

### 项目结构

- `src/extension.ts` — VS Code 客户端入口
- `bundled/tools/lsp_server.py` — Python LSP 服务器（使用 pygls）
- `bundled/tools/ast_parser.py` — 基于缩进的 Ren'Py 解析器
- `syntaxes/` — TextMate 语法高亮文件

## 许可证

ISC 许可证。详见 [LICENSE](LICENSE)。

## 贡献

欢迎在 [GitHub](https://github.com/Zhuxb-Clouds/renpy-support-extension) 提交 Issue 和 Pull Request。
