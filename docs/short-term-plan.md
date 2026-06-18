# Source Reader 短期执行计划（v2）

目标：把 source-reader 收敛成稳定的本机阅读层。优先解决输入能力边界和回归稳定性，再做入口收敛、失败闭环和站点规则增强。

## 当前进度

- 已落地：阶段 1 PDF 本地轻量读取。
- 已落地：阶段 2 Whisper 降级为高级可选能力。
- 已落地：阶段 3 action/feedback/remote 推荐命令收敛到 `read` 主入口；旧入口保留兼容。
- 已落地：阶段 4 status 基于 failure log 输出 Suggestions。
- 待样本驱动：阶段 5 站点规则机制增强。

## 阶段 1：PDF 本地轻量读取

目标：文本型本地 PDF 可读；扫描型、复杂版式和在线 PDF 明确降级。

实施：

- 新增 `reader_core/pdf.py`，默认使用可选依赖 `pypdf`。
- 本地 `.pdf` 文件走 PDF reader，不再按 UTF-8 文本误读。
- `pypdf` 缺失时返回 `partial`，附 `install_pdf_reader` action，建议安装到 `.source-reader/vendor`。
- 扫描型或无可提取文本 PDF 返回 `partial`，提示当前阶段不做 OCR。
- URL PDF 不默认上传在线服务；arXiv PDF 继续转摘要页。
- `status` / `doctor` 增加 PDF reader 状态，但缺失不影响整体健康状态。

不做：

- 不做 OCR。
- 不做表格还原、双栏重排、复杂版式修复。
- 不默认把 PDF 上传给在线模型。
- 不引入 `PyMuPDF` 作为默认依赖。

## 阶段 2：Whisper 降级为高级可选能力

目标：视频读取默认轻量，Whisper 不再被表达成普通必装能力。

实施：

- 视频读取顺序保持：字幕优先，其次在线转写配置，本地 Whisper 作为重型兜底。
- `status` 将 Whisper 标为 `heavy optional`。
- `actions` 区分 `install_yt_dlp`、在线转写配置、本地 Whisper 重型安装。
- 修复测试隔离：无转写能力测试必须同时 mock 掉本机 Groq 配置。

不做：

- 不默认下载 Whisper 模型。
- 不把本地 Whisper 作为安装主路径。
- 不改变已有 `--install-video` 兼容入口。

## 阶段 3：入口收敛

目标：对外只讲 `read / serve / status`，旧入口保留兼容。

实施：

- README、adapter、MCP tool 描述统一到主入口。
- 旧命令不删除，只从主文档中降级为内部兼容入口。
- `build_action_command()` 和服务端 action 统一生成新入口命令。

不做：

- 不静默破坏旧命令。
- 不一次性改 MCP 输出 schema。

## 阶段 4：失败闭环

目标：让 `status` 能解释最近失败，而不是只列 run log。

实施：

- 聚合 `.source-reader/failures/*.json`。
- 按 `domain + strategy + error_type` 统计最近失败。
- 在 `status` 输出 suggestions，例如登录态过期、JS shell、缺依赖、PDF 无文本。

不做：

- 不引入 SQLite。
- 不做大仪表盘。
- 不自动修复、自动登录或自动新增站点规则。

## 阶段 5：站点规则机制增强

目标：只从高频失败样本补规则，避免做大而全爬虫。

实施：

- 从 failure log 选择高频站点。
- 每个规则只处理登录墙识别、JS shell 识别、目标正文判断和后续 action 建议。
- 每个新增站点规则必须有 detector 测试。

不做：

- 不为低频站点写一次性规则。
- 不在规则里写知识库逻辑。

## 回归测试计划

每次代码改动至少跑：

```bash
python3 -m py_compile scripts/source_reader.py scripts/install.py reader_core/pdf.py
python3 scripts/source_reader.py README.md --read-depth preview --format json
python3 scripts/source_reader.py --doctor --format md
python3 scripts/source_reader.py status --format md
python3 -m unittest discover -s tests
```

PDF 专项：

```bash
python3 scripts/source_reader.py <text-pdf> --read-depth preview --format json
python3 scripts/source_reader.py <scan-or-empty-pdf> --read-depth preview --format json
python3 scripts/source_reader.py <broken-pdf> --read-depth preview --format json
```

预期：

- 文本型 PDF：`source_type=pdf`，`strategy=local_pdf_pypdf_text_extraction`。
- 缺少 `pypdf`：`read_quality=partial`，包含 `install_pdf_reader` action。
- 扫描型或空文本 PDF：`read_quality=partial`，包含显式在线解析 action，但不自动上传。
- 损坏 PDF：`read_quality=failed`，错误可读。

视频专项：

```bash
python3 -m unittest tests.test_reader_core.MediaTests
python3 scripts/source_reader.py status --format md
```

预期：

- 无 `yt-dlp`：返回 `partial` 和 `install_yt_dlp` action。
- 有 `yt-dlp` 但无字幕、无 Groq、无 Whisper：返回 `partial`，提示转写能力缺失。
- `status` 中 Whisper 标为 heavy optional。

入口收敛专项：

```bash
python3 scripts/source_reader.py read README.md --read-depth preview --format md
python3 scripts/source_reader.py README.md --read-depth preview --format md
python3 scripts/source_reader.py status --format json
python3 scripts/source_reader.py serve --mcp --help
```

预期：

- 新入口工作。
- 旧入口仍兼容。
- README/adapters 只推荐 `read / serve / status`。

失败闭环专项：

```bash
python3 scripts/source_reader.py status --recent 20 --format md
python3 -m unittest discover -s tests
```

预期：

- 最近失败样本可见。
- suggestions 按失败类型聚合。
- failure log 缺失或损坏时不影响 status。

站点规则专项：

```bash
python3 -m unittest discover -s tests
```

预期：

- 每个新增 `site_rules/<domain>.py` 都有 detector 覆盖。
- 登录墙、JS shell、受限视图误判率可控。
- 没有把站点规则写进知识库流程。
