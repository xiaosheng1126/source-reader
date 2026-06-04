# Source Reader 短期执行计划（v1）

> 时间盒：约 2 周。目标：把"读"做到 **简单、好用、不费 token、聪明一点**。
> 定位：**给 Codex / Claude Code 用的本机阅读层，专读云端 reader 进不来的内容**——登录态网站、内网文档、本地文件、付费订阅。

## 决策已定（不再讨论）

| 决策点 | 选择 |
|---|---|
| 自动升 browser | **默认开**；新增 `--no-auto-upgrade` 兜底。 |
| confidence 阈值 | **写死 40**，跑两周看数据再调，不做 config。 |
| profile 老化提示 | **14 天黄、30 天红**。 |
| 旧 `--install-runtime` flag | **保留 1 个版本**兼容，下下个版本删除。 |
| 节奏 | 单线 PR1 → PR6 顺序推进，每个 PR 改完跑验证、合并、再下一个。 |

## PR 顺序与依赖

```
PR1  confidence + auto-upgrade
   ↓
PR2  history.db (SQLite)
   ↓
PR3  status 命令
   ↓
PR4  profile 健康 + rotate   ──┐ 可并行
PR5  Playwright lazy install ──┘
   ↓
PR6  CLI 收敛 + 文档定位收敛
```

## PR1 — `confidence` 字段 + 自动升 browser

**目标**：失败可见、低质量 fast 读取自动升级到 browser。

**做的事**
- `ReaderOutput` 加 `confidence: int`（0–100），保留 `read_quality` 兼容。
- 新增 `score_confidence(result)`：综合内容长度、headings、auth_wall、js_shell、errors、read_quality 计算。
- `attach_interaction` 统一回填 confidence。
- `classify_and_read`：mode=`fast` 或 `auto` 且 confidence < 40 且 browser 可用时，**自动升级**；metadata 标 `auto_upgraded: true` + 原因。
- 解析默认 browser_profile：未传时回落到 `.source-reader/profiles/default`（存在时）。
- 新增 `--no-auto-upgrade` flag。
- `build_preview`、`to_markdown` 显示 confidence。

**验证**
```bash
python3 -m py_compile scripts/source_reader.py
python3 scripts/source_reader.py README.md --format json | jq .confidence
python3 scripts/source_reader.py --doctor --format md
```

## PR2 — `history.db`（SQLite）

**目标**：读过的可查；为 status / 自动化策略提供数据底座。

**做的事**
- 新文件 `.source-reader/history.db`，schema：
  ```sql
  CREATE TABLE reads(
    id INTEGER PRIMARY KEY,
    url TEXT, source_type TEXT, title TEXT,
    fetched_at TEXT, run_id TEXT UNIQUE,
    mode TEXT, read_depth TEXT,
    confidence INTEGER, content_chars INTEGER,
    auto_upgraded INTEGER, errors_json TEXT
  );
  CREATE INDEX idx_reads_url ON reads(url);
  CREATE INDEX idx_reads_fetched_at ON reads(fetched_at);
  ```
- `persist_run_log` 同时写 JSON + 插入一行。
- 首次启动**回填**已有 `runs/*.json`。
- `classify_and_read` 入口检查 7 天内同 URL 历史，命中时 metadata 加 `history_hit`，**不阻断**。

**验证**
```bash
python3 scripts/source_reader.py README.md --format md
python3 scripts/source_reader.py README.md --format md   # 第二次应有 history_hit
sqlite3 .source-reader/history.db "select count(*) from reads;"
```

## PR3 — `status` 命令

**目标**：一条命令看全状态，告别 `tail log`。

**做的事**
- 新子命令 `python3 scripts/source_reader.py status [--format md|json] [--recent N]`。
- 输出 5 块：
  1. **service**：pid 健康 + 端口 + uptime + 最后心跳。
  2. **profile**：路径、大小、最近成功用它读取的时间；14 天黄、30 天红。
  3. **recent reads**：history.db 最近 N 条（默认 10）。
  4. **playwright**：是否装好、版本。
  5. **runtime**：Python 版本、平台、`.source-reader/` 大小。
- `review-runs` 内部保留，但文档转向 `status --recent`。

**验证**
```bash
python3 scripts/source_reader.py status --format md
python3 scripts/source_reader.py status --format json | jq .
```

## PR4 — Profile 健康 + `profile rotate` + 安全警告

**目标**：登录态可见、不掉、不泄露。

**做的事**
- 新子命令 `python3 scripts/source_reader.py profile <info|rotate> [--name default]`。
  - `info`：路径、大小、上次成功时间、cookie 文件数（粗略）。
  - `rotate`：备份到 `.source-reader/profiles/<name>.bak-<ts>/`，新建空 profile。
- `install.py` 输出尾部、`README.md` browser 段、`status` 输出统一加固定警告：
  > `.source-reader/profiles/` 含登录态等敏感凭据，禁止提交 Git、禁止分享项目目录给他人。

**验证**
```bash
python3 scripts/source_reader.py profile info --format md
python3 scripts/source_reader.py profile rotate --format md
ls -la .source-reader/profiles/
```

## PR5 — Playwright lazy install

**目标**：轻用户不必装 300MB Chromium。

**做的事**
- `install.py` 拆分：
  - `--install-core`（npm install 不跑 chromium）
  - `--install-browser`（运行 `npx playwright install chromium`）
  - 旧 `--install-runtime` = `--install-core --install-browser`，保留 1 版本。
- `read_browser_url` 启动前显式检查 `node_modules/playwright`；未装时返回明确错误 + 安装命令，不再静默失败。
- `--doctor` 区分"browser 模式可用 / 当前未装"。
- README 默认安装命令改为不含 chromium 的版本。

**验证**
```bash
python3 scripts/install.py --install-mcp --start-service     # 不装 Chromium
python3 scripts/source_reader.py <静态网页> --mode fast       # 应正常
python3 scripts/source_reader.py <JS 站> --mode browser ...   # 应明确报"请装 browser"
python3 scripts/install.py --install-browser                  # 装上
python3 scripts/source_reader.py --doctor --format md
```

## PR6 — CLI 收敛 + 定位文案

**目标**：对外只露 3 个入口（`read` / `serve` / `status`）+ 定位陈述就位。

**做的事**
- 文档（README/AGENTS.md/adapters/根 CLAUDE.md）只讲 3 个入口；旧子命令保留但不文档化。
- 新 flags：`read --action`, `read --feedback`, `read --remote`, `serve --mcp`。
- 旧 `mcp` 入口**保留**（install.py 写的 wrapper 仍在用），文档不推荐。
- 各文档首段替换为：
  > source-reader 是给 Codex / Claude Code 用的本机阅读层，专读云端 reader 进不来的内容：登录态网站、内网文档、本地文件、付费订阅。
- 删掉 README 里"通用 reader"暗示和未实现的策略 JSON 段。

**验证**
- 老命令仍工作（兼容性回归）。
- 新命令工作。
- `grep -E "remote-read|review-runs|^action " README.md AGENTS.md` 命中应只在"内部细节"段。

## 总验证（每个 PR 都跑）

```bash
python3 -m py_compile scripts/source_reader.py scripts/install.py
python3 scripts/source_reader.py README.md --read-depth preview --format json
python3 scripts/source_reader.py --doctor --format md
```

## 不在本计划（明确砍掉）

- 缓存层（ROI 没那么大，挪到中期）
- 新站点抽取器、批量读、对比读、主动阅读 Agent
- 隐式反馈数据采集 + domain 策略自学习（中期 P0，需要 PR2 数据底座先到位）
- Profile 加密、多 user namespace、Chrome 扩展
