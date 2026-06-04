# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

**source-reader 是给 Codex / Claude Code 用的本机阅读层，专读云端 reader 进不来的内容：登录态网站、内网文档、本地文件、付费订阅。** 任何让 reader 关心"长期价值/沉淀"的改动都要拒绝——那是上层系统的职责。

## 三个对外入口

```bash
python3 scripts/source_reader.py read <source>       # 默认读
python3 scripts/source_reader.py serve               # HTTP 服务（加 --mcp 起 stdio MCP）
python3 scripts/source_reader.py status              # 服务/profile/最近读取/Playwright/runtime
```

常用 read flag：

```bash
python3 scripts/source_reader.py read <source> --mode fast            # 默认；低 confidence 自动升 browser
python3 scripts/source_reader.py read <source> --no-auto-upgrade      # 关掉自动升级
python3 scripts/source_reader.py read <source> --remote               # 走本机服务
python3 scripts/source_reader.py read <source> --action extract_outline
python3 scripts/source_reader.py read --feedback good --run-id <id>
python3 scripts/source_reader.py read --feedback bad  --run-id <id> --reason "..." --expected "..."
```

Profile 管理：

```bash
python3 scripts/source_reader.py profile info       # 路径/大小/上次 browser 用/健康度
python3 scripts/source_reader.py profile rotate     # 备份当前 profile 并新建空 profile
```

旧子命令（`action`、`feedback`、`review-runs`、`remote-read`、`remote-action`、`mcp`）保留可用，但不再写进文档/adapters。新代码用上面 3 个入口。

安装与注册：

```bash
# 一键：装 Node 依赖 + 写 MCP 配置 + 装 Playwright Chromium + 起服务 + 注册到 Claude
python3 scripts/install.py --install-core --install-mcp --register-mcp claude --start-service

# 只想跳过 ~300MB Chromium 下载（首次升 browser 才装）：
python3 scripts/install.py --install-core --install-mcp --register-mcp claude --start-service --no-browser

# 单独补装 Chromium：
python3 scripts/install.py --install-browser
```

> `--install-mcp` 隐式触发 `--install-browser`（reader 核心定位就吃 browser，开箱即用）。要跳过加 `--no-browser`。`--register-mcp` 取值 `codex` / `claude` / `both` / `none`。

## 改动后必跑

```bash
python3 -m py_compile scripts/source_reader.py scripts/install.py
python3 scripts/source_reader.py read README.md --read-depth preview --format json
python3 scripts/source_reader.py status --format md
python3 scripts/source_reader.py --doctor --format md
```

改了 browser 模式还要在装好 Playwright 的环境跑一次：

```bash
python3 scripts/source_reader.py <url> --mode browser --browser-profile .source-reader/profiles/default --read-depth preview --format md
```

## 架构要点

整个读取器集中在 `scripts/source_reader.py`（~2000 行单文件），按职责分层：

1. **入口分派** — `main()` 先看 argv[0] 是否命中子命令（`action` / `feedback` / `review-runs` / `serve` / `remote-read` / `remote-action` / `mcp`），命中则交给对应 `run_*` 函数；未命中走默认的 source 读取分支。
2. **分类与策略** — `classify_and_read()` 根据 URL host / 路径选 reader：`read_github_*`、`read_video`、`read_discussion`、`read_pdf`、`read_basic_url`，本地路径走 `read_file`。`--mode auto` 时 fast 读到登录墙或 JS 空壳（`looks_like_auth_wall` / `looks_like_js_shell`）会回退到 `read_browser_url`。**交互式终端**（`sys.stdin.isatty()`）下命中 auth_wall 时，`_try_auto_upgrade()` 会自动开 `interactive_login=True` / `headless=False`，省去手动加 `--interactive-login`；MCP / 服务模式（非 tty）不受影响。
3. **Browser 读取** — `read_browser_url()` 通过 `subprocess` 调用 `scripts/browser_reader.mjs`（Playwright 持久化 profile）。Node 侧只负责渲染和导出正文，Python 侧负责截断、打分、构造 ReaderOutput。
4. **输出协议** — 所有 reader 返回 `ReaderOutput` dataclass（`content/preview/actions/next_actions/metadata/errors`）。`attach_interaction()` 统一挂上 `preview` 摘要和 `actions`（每个 action 带 `scope: reader`，可执行命令在 `build_action_command()` 生成）。新增能力必须保持这个协议兼容。
5. **Run log** — `persist_run_log()` 写 `.source-reader/runs/<run_id>.json`，只用于复盘工具表现；不进任何知识库。`status` 的 `recent_reads` 和 `_profile_last_browser_use` 都从这些 JSON 反算，**没有任何 sqlite/缓存层**（避免"上次失败结果被命中"这类反直觉行为）。
6. **本地服务 + MCP** — `SourceReaderHTTPServer` 暴露 `/read` `/action` `/health`，`remote-read` / `remote-action` 是它的客户端。`run_mcp()` 实现 stdio MCP，工具集 `source_reader_read` / `source_reader_action` / `source_reader_feedback` 复用同一套核心函数。服务模式下 `rewrite_actions_for_service()` 会把 action 命令重写成 `remote-action` 形式。

`scripts/install.py` 是 Installer 类，负责创建 `.source-reader/` 目录骨架、写 MCP 运行时文件（`source-reader.runtime.json` / `.codex.toml` / `.claude.json` / wrapper sh）、`npm install` + `npx playwright install chromium`、启动后台服务（pid 文件 + 日志）、可选地把 MCP 注册到 `~/.codex/config.toml` 或 `claude mcp add`。

`adapters/` 下放各 Agent 的接入模板（`adapters/claude/`、`adapters/codex/skills/`），不被 reader 核心引用，只是给上层系统抄的样例。

## 关键约束

- **Token 节省优先**：能读 raw / README / 字幕 / 摘要页就别抓完整外壳；默认走 `fast`，必要时才升 `browser`；默认 `preview` 深度，让调用方判断是否继续。
- **预算阶梯写死在 `READ_DEPTH_BUDGETS`**：preview=6000 / standard=24000 / full=80000。新增 reader 用 `effective_max_chars()` + `cap_text()` 处理截断，不要绕过。
- **登录态隔离**：只用 `.source-reader/profiles/default` 这类独立 profile，绝不复用日常浏览器主 Profile（锁冲突 + 隐私边界）。
- **运行时目录全部 gitignore**：`.source-reader/`（profile、run log、pid、log、MCP 生成文件）和 `node_modules/` 都不进 Git。
- **服务只监听 `127.0.0.1`**：不要改成 `0.0.0.0` 或暴露到局域网。
- **新增 action**：保持 `scope: reader`，命令同步在 `build_action_command()` / `run_action()` 注册；服务模式重写要在 `rewrite_actions_for_service()` 一并处理。
