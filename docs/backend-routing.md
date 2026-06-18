# Backend Routing Plan

目标：借鉴 Agent-Reach 的平台覆盖、后端探测和 doctor 思路，但保留 source-reader 的定位：统一把复杂输入读成稳定的 `ReaderOutput`，不做全局安装器、不写 Agent skill、不接管用户知识库流程。

## 结论

source-reader 应该新增“后端路由层”，而不是把 Agent-Reach 的工具安装和 skill 分发机制照搬进来。

Agent-Reach 强在：

- 平台覆盖面广，显式区分 Web、GitHub、YouTube、Reddit、RSS、Bilibili、Xiaohongshu、LinkedIn、V2EX 等 channel。
- 每个 channel 有 backend 状态，可通过 doctor 解释依赖、登录态、凭据和可用能力。
- 能把平台能力拆成可选工具，而不是所有能力都塞进一个网页抓取路径。

source-reader 应该吸收：

- channel/backend registry。
- backend availability/status。
- doctor 对能力缺口的解释。
- 缺能力时返回 action，而不是直接失败。

source-reader 不应该吸收：

- 自动写全局 Agent skill。
- 自动安装大量外部 CLI。
- 默认读取日常浏览器主 profile cookie。
- 平台工具直接返回给上层 Agent，让输出协议分裂。

## 设计边界

- 对外协议继续是 `ReaderOutput`。
- 现有 `read_quality`、`confidence`、`strategy`、`actions`、`metadata` 字段保持兼容。
- 后端只负责“读入内容”，不决定资料是否沉淀。
- 登录态只使用 `.source-reader/profiles/default` 这类独立 profile。
- 缺少依赖、凭据或登录态时，返回可理解的 partial/blocked 输出和 action。
- 默认仍然节省 token：preview 优先，低成本 backend 优先。

## 目录

当前已落地的路由抽象：

```text
reader_core/backends/
  __init__.py
  base.py
  registry.py
```

第一阶段为了控制改动面，具体 backend reader 仍包在 `scripts/source_reader.py` 里，通过 `FunctionBackend` 注册；等路由行为稳定后，再按文件拆出：

```text
reader_core/backends/
  file.py
  web.py
  github.py
  media.py
  pdf.py
  feed.py
  jina.py
```

第三阶段才考虑强平台规则：

```text
reader_core/backends/
  reddit.py
  v2ex.py
  x.py
  bilibili.py
```

## 核心接口

后端接口保持小，不引入复杂框架：

```python
@dataclass
class BackendStatus:
    id: str
    available: bool
    reason: str = ""
    setup_action_id: str = ""
    quality: str = "optional"


@dataclass
class ReadContext:
    source: str
    mode: str
    read_depth: str
    max_chars: int
    browser_profile: str = ".source-reader/profiles/default"
    interactive_login: bool = False
    allow_external: bool = False


class ReaderBackend(Protocol):
    id: str

    def can_handle(self, source: str) -> bool:
        ...

    def status(self) -> BackendStatus:
        ...

    def read(self, context: ReadContext) -> ReaderOutput:
        ...
```

先让 backend `read()` 直接返回现有 `ReaderOutput`，避免新增第二套结果模型。

## 路由算法

路由器做四件事：

1. 根据输入类型和 URL/domain 选 candidate backend。
2. 根据 `mode`、依赖状态和成本排序。
3. 执行第一个可用 backend。
4. 如果输出 confidence 太低，并且 mode 允许，再尝试下一个 backend。

建议优先级：

```text
local_file / local_pdf
github raw/blob/readme/issue/release
media subtitles
rss feed
fast_web
jina_reader
browser_web
scrapling_web
```

`fast` 模式不主动走 browser/scrapling，除非保留现有 auto-upgrade 行为且检测到 JS shell、登录墙或正文为空。

`browser` 模式只强制使用 browser 相关 backend，不绕回 fast。

`auto` 模式按候选链逐级升级。

## Doctor 输出

doctor 应该从“浏览器/runtime 检查”升级成“能力矩阵”：

| Capability | Backend | Status | Required For | Fix |
|---|---|---|---|---|
| fast web | builtin | ok | static pages | - |
| browser web | Playwright | missing/ok | login and JS pages | install browser / login action |
| GitHub | builtin | ok | repo/blob/issue/release | - |
| video subtitles | yt-dlp | missing/ok | YouTube/Bilibili subtitle | install yt-dlp action |
| PDF text | pypdf | missing/ok | local text PDF | install PDF reader action |
| RSS | builtin/feedparser optional | ok/missing | feed pages | install optional action if needed |
| Jina reader | online | disabled/ok | hard web fallback | explicit enable action |

doctor 只报告状态和修复动作，不自动安装、不修改全局配置。

## Actions 规则

新增或复用 action 时保持 `scope=reader`：

- `install_backend_dependency`：缺可选依赖时给出本项目安装建议。
- `login_with_browser`：需要用户登录独立 profile。
- `read_with_jina`：公开网页读取失败、空壳或被阻断时，显式通过 Jina Reader 外部服务重试。
- `continue_deep_read`：沿用现有深读。
- `extract_outline` / `extract_code`：沿用现有结构化提取。

所有 action 都必须说明风险。涉及上传 URL、页面内容、音视频或 PDF 到外部服务时，默认禁用，必须显式开启。

## 迁移步骤

### Phase 0: 文档固定

- 新增本文件。
- 不改代码。

验证：

```bash
python3 scripts/source_reader.py README.md --read-depth preview --format json
```

### Phase 1: 抽出现有 backend

- 已完成：把现有 `read_basic_url()` 包成 `fast_web` backend。
- 已完成：把现有 browser reader 包成 `browser_web` backend。
- 已完成：把现有 GitHub 读取包成 `github` backend。
- 已完成：把现有视频、本地文件、PDF 读取包成对应 backend。
- 已完成：`classify_and_read()` 先调用 router，保持函数签名不变。
- 已完成：`status` / `doctor` 输出 backend capabilities。

验证：

```bash
python3 -m py_compile scripts/source_reader.py scripts/install.py
python3 scripts/source_reader.py README.md --read-depth preview --format json
python3 scripts/source_reader.py --doctor --format md
python3 scripts/source_reader.py status --format md
python3 -m unittest discover -s tests
```

### Phase 2: 低风险扩展

- 已完成：GitHub repo 增加 `continue_deep_read`：full 模式可读 README、docs 和根目录 manifest。
- 已完成：增加 RSS/Atom backend：使用标准库 XML 解析，不引入 feedparser。
- 已完成：增加 Jina backend：只作为 `read_with_jina` 显式 action，不默认上传内容。

验证：

```bash
python3 -m unittest discover -s tests
python3 scripts/source_reader.py https://github.com/Panniantong/Agent-Reach --read-depth preview --format json
python3 scripts/source_reader.py <rss-url> --read-depth preview --format json
```

### Phase 3: 平台规则增强

- 从 failure log 选择高频平台，不按清单盲目扩。
- 每个平台只补正文定位、受限视图检测、登录态提示和 action。
- 每个新增规则必须有 detector 测试。

验证：

```bash
python3 -m unittest discover -s tests
python3 scripts/source_reader.py status --recent 20 --format md
```

## 不做事项

- 不把 Agent-Reach 作为运行时硬依赖。
- 不自动执行 `agent-reach install`。
- 不把 platform CLI 的原始输出直接暴露给 MCP client。
- 不新增默认在线上传路径。
- 不为了平台覆盖牺牲 `ReaderOutput` 的稳定性。

## 成功标准

- 现有读取结果兼容。
- doctor 能解释每类能力为什么可用或不可用。
- 新平台能力都能降级，不因缺依赖导致核心读取崩溃。
- preview 仍然是默认低成本路径。
- 后续新增 backend 不需要修改一大段 `classify_and_read()` 分支。
