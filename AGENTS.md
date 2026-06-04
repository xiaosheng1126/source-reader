# Source Reader 项目规则

## 项目定位

**source-reader 是给 Codex / Claude Code 用的本机阅读层，专读云端 reader 进不来的内容：登录态网站、内网文档、本地文件、付费订阅。**

它只负责"如何把复杂输入读进来"，不负责知识库流程，不决定资料是否沉淀，不写 raw/wiki，不维护用户知识结构。

## 目录约定

- `scripts/source_reader.py`：Python CLI、HTTP service、MCP server、读取策略入口。
- `scripts/browser_reader.mjs`：Playwright 持久化 profile 网页读取器。
- `scripts/install.py`：本地运行时、MCP wrapper、Codex/Claude 注册安装器。
- `adapters/`：面向不同 Agent 的接入说明和 skill/command 模板。
- `docs/`：输入类型、策略设计和后续规划。
- `.source-reader/`：运行时目录，只存 profile、run log、pid、log、MCP 生成文件，不提交 Git。

## 开发约束

- 不把 Karpathy KB、Obsidian raw/wiki、个人知识库状态写进 reader 核心。
- 新增能力先放在读取策略层，保持输出协议兼容。
- `actions` 只表达 reader 可执行操作；上层系统需要沉淀时自行适配。
- 默认先节省 token：preview 优先，能读 raw/README/字幕/摘要页就不抓完整外壳。
- 登录态只使用 `.source-reader/profiles/default` 这类独立 profile，不复用日常浏览器主 profile。

## 验证

改动后至少执行：

```bash
python3 -m py_compile scripts/source_reader.py scripts/install.py
python3 scripts/source_reader.py README.md --read-depth preview --format json
python3 scripts/source_reader.py --doctor --format md
python3 scripts/source_reader.py status --format md
```

如果修改了 browser 模式，还需要在已装 Playwright 的环境执行：

```bash
python3 scripts/source_reader.py <url> --mode browser --browser-profile .source-reader/profiles/default --read-depth preview --format md
```
