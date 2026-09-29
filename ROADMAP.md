# Ren'Py LSP 迭代路线图

基于 v1.5.0 现状的后续迭代计划，按优先级排序。

## 当前已实现（v1.8.0）

- **模块化服务端**：`lsp_server.py`（原 ~3550 行）拆分为 `server_context`（共享状态）、`diagnostics`（诊断计算/发布/后台调度）、`completion`（上下文补全）、`code_actions`（快速修复）
- **快速修复（Code Actions）**：未定义 label 一键创建、未使用 label 一键删除块（按诊断 `code` 匹配）
- **引用诊断**：`show`/`scene`/`hide` 未定义 image（内置 `black`/`white`/`window` 与 tag 前缀匹配豁免）、`at` 未定义 transform（内置方位/define/default/内联表达式豁免）、`style x is parent` 未知父样式（内置样式表白名单）
- 所有诊断带机器可读 `code` 字段

- 文档符号、折叠、跳转定义、悬停、格式化、引用、颜色、重命名
- **上下文补全**：label（jump/call）、transition（with）、transform（at）、screen、image（含 images/ 自动检测）、audio define、关键字/角色
- **补全扩展**：define/default 变量、style 名、screen displayable/property、transform/ATL 语句、style 属性
- **语义诊断**：未定义 jump/call label、重复 label/screen 定义、缺失图像文件、未使用 label
- 工作区索引、翻译 ID 支持、解析器错误恢复（Unknown 节点）
- pytest 测试基座（ast_parser、WorkspaceIndex、LSP completion）
- `label`、`screen`、`menu`、`define` snippets
- 11 个 TextMate grammar（renpy、screen、style、atl、test、python、glsl 及各类注入）

## 阶段一：补全 LSP 核心能力（性价比最高）

- [x] **pytest 测试**：为 `ast_parser`（1413 行手写解析器）与 LSP 特性建立测试基座（基于 `test_samples/` 扩展）。4700 行 Python 零测试，必须先做再叠加新特性，否则每加一个分支都在积累回归风险
- [x] **补全缺口**：`define`/`default` 变量补全、`style` 名补全、`transform` 语句名补全、screen 内属性补全
- [ ] **Signature help**：`show`/`call`/`with`/`play music` 等语句的参数提示
- [x] **Code actions**：诊断快速修复——未定义 label 一键创建（v1.8.0）；未使用 label 一键删除块（v1.8.0）
- [x] **模板代码片段**：`label`、`screen`、`menu`、`define` 的 snippet（纯 `package.json` 声明，零 LSP 成本，半天工作量）

## 阶段二：Ren'Py 特有深度功能

- [ ] **诊断缺口**：~~`show`/`scene` 引用未定义 image、`at` 引用未定义 transform、`style` 引用未定义 style~~（v1.8.0 已完成）、未使用 define/default 变量
- [ ] **翻译 `old`/`new` 一致性诊断**：原文改动后 `old` 字符串失配检测（翻译者最大痛点，已有翻译 ID hover，自然延伸，差异化价值高）
- [ ] **Inlay hints / Code lens**：变量类型推断、label 被引用次数（锦上添花，低优先级）
- [ ] **ATL / screen 语法专门支持**：LSP 层解析 screen 内部语法（大工程：use、transclude、python 块、位置属性等）
- [ ] **Semantic tokens**：语义级高亮修正 regex TextMate 高亮误判（中等优先级）

## 阶段三：可靠性与性能

- [x] **性能测试基座**（v1.8.0）：`tests/test_performance.py`（`perf` marker）覆盖解析吞吐与 2× 伸缩、百文件索引预热、冷/热全量诊断、补全延迟、格式化吞吐；实测百文件项目热诊断 ~2ms。后续调优先跑 `pytest tests/test_performance.py -q -s` 看数据
- [ ] **增量解析**：避免大项目全量重解析
- [ ] **索引持久化（先测量后决定）**：工作区扫描已有 4-worker 线程池，典型项目未必是瓶颈；先用 `showStats` 收集真实项目数据，确认瓶颈后再引入落盘缓存（会带来失效/版本一致性问题）
- [ ] **错误恢复增强**：块结构错误（缩进错误、缺失冒号）时的 AST 保留


## 建议执行顺序

阶段一 → 阶段二 → 阶段三

阶段一优先建立测试基座，前两个阶段直接提升用户感知，阶段三保证可持续迭代。
