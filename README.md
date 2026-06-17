# Source Reader

**source-reader 是给 Codex / Claude Code 用的本机阅读层，专读云端 reader 进不来的内容：登录态网站、内网文档、本地文件、付费订阅。**

它只解决"如何把复杂输入读进来"，不维护知识库状态，不决定资料是否沉淀，不写 raw/wiki。上层 Agent / 知识库自己消费 reader 输出再决定如何处理。

## 边界

- 做：识别输入类型、选择低成本读取策略、处理 JS 渲染/登录态、输出正文/元数据/质量/下一步操作。
- 不做：决定资料是否值得长期保存、写 raw、更新 wiki、维护用户知识结构。

## V1 支持范围

- 普通 URL：静态网页、博客、官方文档。
- JS 渲染网页：用 Playwright 持久化 profile 渲染后读取正文。
- 登录态网页：用 Playwright 持久化 profile 保存登录态后读取。
- GitHub：repo、gist、issue、PR、release note、raw 文件。
- 中文技术社区：掘金登录/受限视图识别。
- PDF/论文：arXiv PDF 优先读取摘要页；普通 PDF 先标记为待增强。
- 视频字幕：YouTube、B站。
- 讨论串：HN、Reddit、V2EX、X thread。
- 本地输入：Markdown、TXT、HTML、截图、聊天记录、粘贴文本。

## Token 节省策略

- 能读 raw 就不读网页外壳。
- 能读 README 就不遍历仓库。
- 能读字幕就不处理音视频本体。
- 能读摘要页就不直接读取整篇 PDF。
- 能读主帖和少量高价值评论，就不拉完整讨论串。
- 默认先走 `fast`，只有登录墙或 JS 空壳才用 `browser/auto`。
- 默认 `max_chars=24000`，超出时保留头部和尾部，中间明确标记截断。
- 支持 `--read-depth preview|standard|full`，先用 `preview` 快速判断是否值得继续。

## 三个入口

对外只露这三个：

```bash
python3 scripts/source_reader.py read <source>       # 直接读
python3 scripts/source_reader.py serve               # 启动本机 127.0.0.1 HTTP 服务
python3 scripts/source_reader.py status              # 服务/profile/最近读取/Playwright/runtime 一览
```

`read <source>` 是默认入口，常用 flag：

| Flag | 含义 |
|---|---|
| `--mode fast\|browser\|auto` | 读取策略（默认 `fast`，confidence 不足时自动升 browser） |
| `--read-depth preview\|standard\|full` | 预算（默认 `standard`；大资料先 `preview`） |
| `--remote` | 走本机服务（Agent 沙箱推荐） |
| `--action <id>` | 执行 action（`continue_deep_read` / `extract_outline` / `extract_code` / `login_with_browser`） |
| `--feedback good\|bad --run-id ... [--reason ... --expected ...]` | 记录反馈 |
| `--interactive-login` | browser 模式下等待人工登录 |
| `--no-auto-upgrade` | 关闭"低 confidence 自动升 browser" |
| `--format md\|json` | 输出格式 |
| `--doctor` | 检查 Node/npm/Playwright/profile |

`serve` 子选项：

| Flag | 含义 |
|---|---|
| `--mcp` | 改起 stdio MCP server（供 Codex/Claude 的 MCP 客户端用） |
| `--port` / `--host` | HTTP 端口（默认 8765） |

`status` 子选项：

| Flag | 含义 |
|---|---|
| `--recent N` | 最近 N 次读取（默认 10） |
| `--format md\|json` | 输出格式 |

读取策略：

- `fast`：HTTP 读取，成本最低；confidence < 40 或检测到登录墙/JS 空壳时**自动升 browser**（前提：Playwright 已装、profile 存在）。
- `browser`：强制 Playwright 持久化 profile，适合语雀、飞书、Notion、JS 渲染站点。
- `auto`：和 fast 行为相同（保留为别名）。

## 本地服务与 Agent 接入

安装时推荐一次性准备运行时并启动服务：

```bash
python3 scripts/install.py --install-core --install-mcp --start-service
```

这会准备：

- Node/npm 项目依赖（`--install-core`）。
- `.source-reader/profiles/default` 登录态目录。
- `.source-reader/mcp/` MCP 模板和运行时元数据。
- `.source-reader/source-reader.pid` 服务 PID。
- `.source-reader/source-reader.log` 服务日志。

Playwright Chromium（约 300MB）默认**不装**。需要 browser/auto 模式时再补：

```bash
python3 scripts/install.py --install-browser
```

yt-dlp 默认**不装**。读取 YouTube 等视频字幕时，如果输出提示缺失，再按需安装到项目本地 `.source-reader/vendor`：

```bash
python3 scripts/install.py --install-yt-dlp
```

服务只监听 `127.0.0.1`，不会暴露到局域网。Codex / Claude / MCP 走：

```bash
python3 scripts/source_reader.py read <source> --remote --read-depth preview --format md
python3 scripts/source_reader.py read <source> --remote --action continue_deep_read --format md
```

支持 MCP 的客户端用 `serve --mcp`：

```bash
python3 scripts/source_reader.py serve --mcp
```

安装器会在 `.source-reader/mcp/source-reader.runtime.json` 写入当前 source-reader 项目的绝对路径、MCP 命令和本地服务端口；`source-reader.codex.toml`、`source-reader.claude.json` 是接入 Codex / Claude 的配置片段。

确认要注册到当前机器的全局 Agent 配置时执行：

```bash
python3 scripts/install.py --install-core --install-mcp --register-mcp both --start-service
```

`--register-mcp codex` 会备份并更新 `~/.codex/config.toml`；`--register-mcp claude` 会调用 `claude mcp add --scope user source-reader`。已有同名 MCP 时默认跳过，替换时使用 `--force`。

MCP tools:

- `source_reader_read`
- `source_reader_action`
- `source_reader_feedback`

只有服务未启动、用户首次登录、验证码、账号权限不足、登录态过期这类必须用户参与的情况，才需要用户介入。

## 阅读深度

- `preview`：默认预算 6000 字符，输出标题、结构、前导片段和下一步动作，适合先判断资料价值。
- `standard`：默认预算 24000 字符，适合普通总结和任务分析。
- `full`：默认预算 80000 字符，适合用户确认后的深读；仍然保留截断标记，避免失控读取。

如果显式传入 `--max-chars`，以 `--max-chars` 为准。

## 下一步操作协议

`source_reader.py` 会在 JSON 输出里提供 `preview`、`actions` 和兼容字段 `next_actions`，在 Markdown 输出里显示 `Quick Preview` 和 `Next Operations`。

每个 action 都带有 `scope`：

- `reader`：source-reader 核心动作，可以被任何客户端直接使用。

当前动作包括：

- `continue_deep_read` (`reader`)：用 `--read-depth full` 重新读取。
- `extract_outline` (`reader`)：只提取结构、大纲、关键概念和内容地图。
- `extract_code` (`reader`)：只提取代码、命令、配置、API 示例和集成步骤。
- `ask_followup` (`reader`)：保留给用户针对章节、实现、风险继续提问。
- `login_with_browser` (`reader`)：当读取被登录墙或错误阻断时出现，使用 Playwright 持久化 profile 重新读取。
- `mark_result_good` / `mark_result_bad` (`reader`)：记录这次读取是否满足预期，用于后续复盘。

这套“操作”先以稳定数据协议存在，后续可以映射到聊天 UI、Obsidian 命令、Raycast 或快捷指令。

动作通过 `read --action <id>` 触发：

```bash
python3 scripts/source_reader.py read <source> --action continue_deep_read --format md
python3 scripts/source_reader.py read <source> --action extract_outline --format md
python3 scripts/source_reader.py read <source> --action extract_code --format md
python3 scripts/source_reader.py read <source> --action login_with_browser --format md
```

加 `--remote` 走本机服务：

```bash
python3 scripts/source_reader.py read <source> --remote --action continue_deep_read --format md
```

## 复盘和反馈

每次普通读取和 action 执行都会写入轻量 run log：

```text
.source-reader/runs/<run_id>.json
```

run log 只用于复盘工具表现，不进入任何知识库。它记录输入、读取策略、读取质量、token 估算、actions 和用户反馈。

当读取结果为 `failed` / `blocked` / `partial`，或结果中包含 `errors` 时，会额外写入失败样本：

```text
.source-reader/failures/<run_id>.json
```

failure log 保存本次调用参数、读取质量、策略、结构化 metadata、错误和正文前 2000 字符，用于后续定期复盘站点规则、登录态判断和降级策略。该目录同样属于本机运行时数据，不提交 Git。

反馈命令：

```bash
python3 scripts/source_reader.py read --feedback good --run-id <run_id>
python3 scripts/source_reader.py read --feedback bad --run-id <run_id> --reason "正文不完整" --expected "希望读到正文而不是导航"
```

近期读取摘要走 `status`：

```bash
python3 scripts/source_reader.py status --recent 20 --format md
```

第一次使用 browser profile 时，需要让 Playwright 打开可见 Chrome，手动登录目标站点。后续同一 profile 会复用登录态。不要直接使用日常 Chrome 主 Profile，避免锁冲突和隐私边界不清。

> [security] `.source-reader/profiles/` 含登录态等敏感凭据，禁止提交 Git、禁止分享项目目录给他人。需要重置时执行 `python3 scripts/source_reader.py profile rotate`，旧 profile 会以 `<name>.bak-<ts>/` 备份保留。

如果页面跳到登录页，使用 `--interactive-login`。工具会等待你在打开的浏览器里扫码或账号登录，然后继续抽取正文。

如果 browser 模式失败，先运行 `python3 scripts/source_reader.py --doctor --format md`。doctor 会检查 Node、npm、Playwright、browser reader 脚本和持久化 profile，并给出下一步命令。

Playwright 是可选依赖；需要 browser 模式时单独安装：

```bash
python3 scripts/install.py --install-browser
```

yt-dlp 也是可选依赖；只影响视频字幕读取，不影响网页、PDF、GitHub、browser 模式：

```bash
python3 scripts/install.py --install-yt-dlp
```

## 统一输出

```yaml
source_id:
input_type:
source_type:
status:
title:
url:
local_path:
author:
published_at:
fetched_at:
reader:
read_quality:
metadata:
assets:
errors:
```

正文输出应包含：

- 读取质量说明
- 原始内容或可追溯摘录
- 快速预览
- 下一步操作
- 错误和降级信息
