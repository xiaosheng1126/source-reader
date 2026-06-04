# Source Reader

source-reader 是独立的智能内容读取器，负责把各种输入读取成 LLM 可用的结构化结果。它可以被知识库、开发任务、代码审查、资料分析或其他 Agent 复用，但自身不维护个人知识库状态。

## 为什么独立

读取 URL 和读取知识不是同一件事。

- source-reader 解决：如何把复杂输入读进来。
- knowledge-base 解决：这些信息对用户有什么长期价值。

这样 `读取 <source>` 可以只用于当前任务，`沉淀 <source>` 才进入知识库流程。

source-reader 的边界：

- 做：识别输入类型、选择低成本读取策略、处理 JS 渲染/登录态、输出正文/元数据/质量/下一步操作。
- 不做：决定资料是否值得长期保存、写 raw、更新 wiki、维护用户知识结构。
- 可以被上层系统适配：例如知识库系统可以消费 reader 输出再决定是否写入 raw/wiki，但这不是 reader 核心职责。

## V1 支持范围

- 普通 URL：静态网页、博客、官方文档。
- JS 渲染网页：用 Playwright 持久化 profile 渲染后读取正文。
- 登录态网页：用 Playwright 持久化 profile 保存登录态后读取。
- GitHub：repo、gist、issue、PR、release note、raw 文件。
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

## 读取模式

```bash
python3 scripts/source_reader.py <url> --mode fast --format md
python3 scripts/source_reader.py <url> --mode browser --browser-profile .source-reader/profiles/default --format md
python3 scripts/source_reader.py <url> --mode auto --browser-profile .source-reader/profiles/default --format md
python3 scripts/source_reader.py <url> --mode browser --browser-profile .source-reader/profiles/default --interactive-login --format md
python3 scripts/source_reader.py <url> --mode auto --browser-profile .source-reader/profiles/default --interactive-login --login-timeout-ms 180000 --read-depth preview --format md
python3 scripts/source_reader.py <url> --mode auto --read-depth preview --format md
python3 scripts/source_reader.py serve --host 127.0.0.1 --port 8765
python3 scripts/source_reader.py remote-read <url> --read-depth preview --format md
python3 scripts/source_reader.py mcp
python3 scripts/source_reader.py --doctor --format md
```

- `fast`：默认模式，HTTP 读取，成本最低。
- `browser`：强制使用 Playwright 持久化 profile，适合语雀、飞书、Notion、JS 渲染站点。
- `auto`：先 `fast`，如果检测到登录墙或 JS 空壳，再切到 `browser`。配合 `--interactive-login` 时，不需要先询问用户是否重试；工具会直接打开持久化浏览器等待登录。
- `serve`：启动本机 `127.0.0.1` source-reader 服务。外部联网、Playwright、缓存和登录态由服务进程负责。
- `remote-read`：Agent 日常优先使用这个入口，只访问本机服务，不直接由 Agent 沙箱访问公网。
- `mcp`：启动 stdio MCP server。支持 MCP 的 Agent 应优先使用这个入口，避免把外部读取做成普通 shell 网络命令。

## 本地服务

安装时推荐一次性准备运行时并启动服务：

```bash
python3 scripts/install.py --install-runtime --install-mcp --start-service
```

这会准备：

- Node/npm 项目依赖。
- Playwright Chromium。
- `.source-reader/profiles/default` 登录态目录。
- `.source-reader/mcp/` MCP 模板和运行时元数据。
- `.source-reader/source-reader.pid` 服务 PID。
- `.source-reader/source-reader.log` 服务日志。

服务只监听 `127.0.0.1`，不会暴露到局域网。后续 Codex / Claude / MCP 应优先调用：

```bash
python3 scripts/source_reader.py remote-read <source> --read-depth preview --format md
python3 scripts/source_reader.py remote-action continue_deep_read --source <source> --format md
```

支持 MCP 的客户端可以使用：

```bash
python3 scripts/source_reader.py mcp
```

安装器会在 `.source-reader/mcp/source-reader.runtime.json` 写入当前 source-reader 项目的绝对路径、MCP 命令和本地服务端口；`source-reader.codex.toml`、`source-reader.claude.json` 是接入 Codex / Claude 的配置片段。

确认要注册到当前机器的全局 Agent 配置时执行：

```bash
python3 scripts/install.py --install-runtime --install-mcp --register-mcp both --start-service
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

动作可以直接执行：

```bash
python3 scripts/source_reader.py action continue_deep_read --source <source> --format md
python3 scripts/source_reader.py action extract_outline --source <source> --format md
python3 scripts/source_reader.py action extract_code --source <source> --format md
python3 scripts/source_reader.py action login_with_browser --source <source> --format md
```

服务模式下优先使用：

```bash
python3 scripts/source_reader.py remote-action continue_deep_read --source <source> --format md
python3 scripts/source_reader.py remote-action extract_outline --source <source> --format md
python3 scripts/source_reader.py remote-action extract_code --source <source> --format md
python3 scripts/source_reader.py remote-action login_with_browser --source <source> --format md
```

## 复盘和反馈

每次普通读取和 action 执行都会写入轻量 run log：

```text
.source-reader/runs/<run_id>.json
```

run log 只用于复盘工具表现，不进入任何知识库。它记录输入、读取策略、读取质量、token 估算、actions 和用户反馈。

反馈命令：

```bash
python3 scripts/source_reader.py feedback mark_good --run-id <run_id>
python3 scripts/source_reader.py feedback mark_bad --run-id <run_id> --reason "正文不完整" --expected "希望读到正文而不是导航"
```

近期复盘：

```bash
python3 scripts/source_reader.py review-runs --limit 20 --format md
```

source-reader 可以基于 run log 给出规则建议，但不自动修改代码、全局配置或上层知识库。策略变更需要用户确认。

第一次使用 browser profile 时，需要让 Playwright 打开可见 Chrome，手动登录目标站点。后续同一 profile 会复用登录态。不要直接使用日常 Chrome 主 Profile，避免锁冲突和隐私边界不清。

如果页面跳到登录页，使用 `--interactive-login`。工具会等待你在打开的浏览器里扫码或账号登录，然后继续抽取正文。

如果 browser 模式失败，先运行 `python3 scripts/source_reader.py --doctor --format md`。doctor 会检查 Node、npm、Playwright、browser reader 脚本和持久化 profile，并给出下一步命令。

Playwright 是可选依赖；需要 browser 模式时在项目目录安装：

```bash
python3 scripts/install.py --install-runtime --install-mcp --start-service
```

## 策略配置方向

source-reader 后续可以支持轻量策略配置，但配置只影响“怎么读”，不影响“要不要沉淀”。

建议配置文件：

```json
{
  "default_read_depth": "preview",
  "max_chars": {
    "preview": 6000,
    "standard": 24000,
    "full": 80000
  },
  "browser_profile": ".source-reader/profiles/default",
  "domains": {
    "github.com": {
      "repo_strategy": "readme_first",
      "issue_comment_limit": 12
    },
    "yuque.com": {
      "mode": "auto",
      "prefer_browser_profile": true
    }
  }
}
```

短期只建议加入这些策略：默认读取深度、每层 token 预算、域名级读取模式、GitHub issue/PR 评论上限、登录态 profile。不要把用户偏好、知识分类、wiki 目标写进 source-reader 配置。

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
