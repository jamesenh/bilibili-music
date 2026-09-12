# AGENTS.md — 编码规范与项目方向（强制约束）

> **本文件对在本仓库中工作的所有编码 Agent（含 DSH）具有强制力。**
>
> 只要任务涉及本目录下的代码，就必须先完整读完本文件再动手。规则使用「必须 / 禁止 /
> 一律」表述，**没有"视情况"的余地**。任何一条无法遵守时，按文末
> [规则冲突与豁免流程](#规则冲突与豁免流程) 处理，**禁止沉默违反**。
>
> 本文件优先级高于 Agent 的默认习惯与个人偏好；与 `README.md` 冲突时以本文件为准。

---

## 0. 三条不可交易的红线

1. **中文注释与 docstring**：新增或修改的模块、类、函数、方法、属性一律要有中文
   docstring，关键方法必须有中文注释说明思路（详见 [第 2 节](#2-注释与-docstring硬性要求)）。
2. **分层不破**：`core`（纯逻辑）之外的分层依赖只能单向向下，禁止反向依赖与跨层捷径
   （详见 [第 4 节](#4-架构与分层必须遵守)）。
3. **改完必验**：动了代码就**必须**跑测试并汇报真实结果，测试红着不许交付
   （详见 [第 7 节](#7-验证与交付-验收标准)）。

---

## 1. 项目定位与方向（用来判断"这算不算跑偏"）

**一句话**：BiliMusic 是一个**基于 PySide6 从零自研的 B 站音乐视频桌面客户端**，
把音乐区的视频/合集当作歌单来搜、来播、来缓存。

**当前阶段**：MVP 端到端已跑通（搜索 → 分P → 音轨 → 缓存 → 播放），两种网络后端可互换。

**后续做什么看哪份文档**：下一阶段做什么、依赖顺序、以及**已拍板的决策**（例如账号能力
当前冻结）一律见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。**不要在路线图之外自行扩范围**；
本文件第 1.4 节的跑偏清单优先于路线图里的任何登记项。

### 1.1 技术栈（锁定，不要擅自替换）

| 项 | 取值 | 说明 |
|---|---|---|
| GUI | PySide6 **6.8.3** | 只做桌面，不做 Web/TUI |
| Python | **3.11**（`.python-version`） | `requires-python = ">=3.11,<3.14"`，**不能用 3.14** |
| 依赖管理 | `uv` | 新增依赖前先问；能用 Qt 自带的就不加包（但 `core` 不许 import Qt，见 4.1；`json` 这类标准库方案同样不新增依赖） |
| 网络 | `QNetworkAccessManager`（默认）+ `urllib`（对照） | 两者接口必须一致，可 `--backend` 切换 |
| 播放 | `QMediaPlayer` + `QAudioOutput` | 只播**本地音频文件** |
| 本地存储 | 文件缓存（`core/cache.py`，原子写入） | 不引入数据库/ORM |

### 1.2 核心流程（固定，不要绕开）

```
搜索(/search/type) → 选中 → 补详情(/view) → 选分P(cid) → playurl 取音轨
→ 选音质 → 流式下载到本地缓存 → 播放本地文件
```

**播放本地文件而不是 CDN 直链**：直链带签名会过期，且 `QMediaPlayer` 的媒体请求
不经过我们的网络层，无法附加防盗链必需的 `Referer`，实测会被 CDN 拒绝。这条结论是
实测踩出来的，**不要"优化"成直接播远程 URL**。

### 1.3 领域铁律

音乐区的多P合集**到处都是**（`【全MV合集】【200P】`），**每个分 P 就是一首歌**：

- 展示时长一律用分 P 自己的 `duration`（视频级的 `duration` 是**所有分P之和**）
- 播放一律用分 P 的 `cid`（视频级 `cid` 只是第 1P）
- 缓存键一律包含 `cid`（否则不同分P会串歌）

### 1.4 当前**不做**的事（跑偏清单，除非用户当轮明确要求）

- 登录、Cookie 导入、扫码、账号体系
- WBI 签名（`w_rid`/`wts`）、收藏夹云同步、云端歌单
- 弹幕 / 评论 / 字幕抓取
- 视频画面播放、转码、视频下载
- 引入 QML / WebEngine / 第三方播放器或下载器库
- 后端服务、账号系统、多端同步、内置 Web 服务器

> 判断标准：**与"搜到歌 → 存到本地 → 放出来"这条主线无关的功能，先不做，先问。**

---

## 2. 注释与 docstring（硬性要求）

> 这是本仓库最容易被忽略、也最被在意的一条。**"代码能跑"不构成省略注释的理由。**

### 2.1 覆盖范围：必须写

- **模块**：每个 `.py` 文件顶部必须有模块 docstring，说明**这个模块负责什么 + 关键设计取舍**。
- **类**：包括 `@dataclass`、`Protocol`、异常类、内部状态类。
- **函数与方法**：公开的、私有的（`_foo`）、`__init__`、`@property` 一律要有。
- **属性**：语义不直观的 `@property` 要有 docstring。
- **模块级常量**：非平凡常量用 `#:` 注释说明用途（现有代码风格即如此）。

**唯一豁免**：单行、语义自明的 `@property` / 转发方法可以不写 docstring —— 但只要有
任何判断、转换、兜底逻辑，就必须写。

### 2.2 写法：中文 + Google 风格

docstring **必须用中文陈述**。专有名词、参数名、B站接口字段名、Qt 类型名保留原文，
不要硬译（写 `QNetworkAccessManager`、`cid`、`fnval=16`、`buvid3`，不要写"网络访问管理器"）。

```python
def pick_best_cached(cache: AudioCache, video: Video, page: Page) -> CachedHit | None:
    """盲查缓存:不知道实际音质时,按常见档位从高到低试。

    用于在请求 playurl **之前**预判(命中就能直接开播,省掉整次网络请求)。

    Args:
        cache: 音频缓存实例。
        video: 目标视频,提供 ``bvid``。
        page: 目标分P,提供 ``cid``。

    Returns:
        命中的档位、codec 与文件路径;全部档位都未命中时返回 ``None``。
    """
```

约定：

- 首行为一句话摘要，**以句号结尾**；空一行后写详细说明；再空一行写 `Args` / `Returns` /
  `Raises` / `Yields`（按需，用到的才写）。
- **会抛异常的函数必须写 `Raises`**；可能返回 `None` 的必须写清楚什么时候 `None`。
- 参数说明不需要重复类型（类型在签名里），要写**语义、约束、单位、取值来源**。
- 中文标点与英文术语之间**不加多余空格**，与仓库现有代码保持一致。

### 2.3 禁止事项（评审必查）

- **禁止空话**：`"""初始化。"""`、`"""获取数据。"""`、`"""处理一下。"""` 等于没写。
- **禁止复读代码**：`x = x + 1  # 把 x 加 1` 这种注释一律删掉。
- **禁止用 TODO 代替说明**：可以留 `# TODO(原因):`，但必须写清**为什么先不做**。
- **禁止英文 docstring**：包括从别处抄来的。翻译成中文，术语可保留原文。
- **禁止注释与实现不一致**：改了行为必须同步改注释与 docstring，**过时注释比没有更糟**。
- **禁止残留调试代码**：提交前清掉 `print` 调试、被注释掉的旧实现、`breakpoint()`。

### 2.4 注释要写"为什么"

代码说明**做了什么**，注释说明**为什么这么做**。以下情况**必须**有中文注释：

- 反直觉的写法或兜底分支（例如"分P缺失时退回视频级 cid"）
- 平台差异（Windows / macOS / Linux 的路径、编码、插件差异）
- 性能取舍（流式解压、分块大小、避免全量进内存）
- 超时 / 重试 / 退避 / 限速的数值来源（风控实测结论）
- 外部约束（B站接口坑、CDN 防盗链、Qt 行为限制）

### 2.5 分节与结构

- 类内用 `# ------------------------------------------------------------ 小节名`
  分隔逻辑区块（现有代码风格）。
- 模块内用 `# ============================== 大节名` 分隔更大区块。
- 文件末尾主动维护 `__all__`（公开接口一目了然，也避免 `import *` 污染）。

---

## 3. 命名与代码风格

- **命名**：模块/函数/变量 `snake_case`；类/Protocol `PascalCase`；常量 `UPPER_SNAKE`；
  模块级私有 `_leading_underscore`；回调参数以 `on_` 开头（`on_success` / `on_error` /
  `on_progress`）。
- **类型注解**：公开接口必须写全。文件首行 `from __future__ import annotations` 不可省。
- **导入顺序**：标准库 → 第三方（PySide6）→ 项目内（`from ..core.xxx import`），
  组间空行分隔。
- **数据结构**：数据模型一律 `@dataclass(slots=True)`；只需要契约时用
  `typing.Protocol`，不要为了继承引抽象基类。
- **异常**：只抛 `core/errors.py` 里的异常体系（`BiliMusicError` 及其子类）；禁止裸
  `raise Exception(...)` / `raise RuntimeError(...)`。
- **项目内引用一律相对导入**（`from ..core.errors import ...`），禁止把 `src` 路径塞进
  `sys.path`（`tests/` 与 `scripts/` 的引导除外）。
- **Qt 枚举写全限定名**（`QHeaderView.ResizeMode.Stretch`），不要用已废弃的短名。
- **禁止在 Qt 路径里 `sleep` 阻塞事件循环**；定时一律 `QTimer`。既有的 `time.sleep`
  只出现在同步的 `net/urllib_client.py`（它没有事件循环，退避与限速只能阻塞等待），
  **不要**把这个写法照搬进任何 Qt 代码路径。
- 新增依赖前先确认 Qt 自带能力不够用，并**先征得用户同意**。

---

## 4. 架构与分层（必须遵守）

```
core/   与 UI 无关的纯逻辑,可独立单测(不 import Qt,不认识 B站业务之外的 UI)
  ↑
net/    网络后端(两种实现,同一个 Protocol 契约)
  ↑
api/    接口封装 + 纯解析函数(parse_*)
  ↑
audio/  解析状态机(AudioResolver) + 播放器封装(PlayerController)
        + 播放编排(PlaybackController,串起队列/解析/播放)
  ↑
ui/     界面(依赖以上全部)
```

**单向依赖，只允许向下引用。**

- 禁止 `core` → `net` / `api` / `audio` / `ui`；禁止 `api` → `ui`；以此类推。
- `net/` 里 Qt 与 urllib **两种后端必须接口一致**，改动一个必须同步另一个。
- **解析与请求分离**：`parse_*` 必须是纯函数（输入原始 JSON，输出数据模型，不碰网络、
  不碰 Qt），这样接口解析能用固定样本单测、不触网。
- UI 里禁止写业务逻辑与数据解析；UI 只做展示与事件转发。
- **界面层不要线程**：网络全部是 QNAM 信号回调，UI / `audio` / Qt 后端里**禁止新增
  `QThread` 或 `threading` 线程**，除非用户明确要求并说明理由。（`net/urllib_client.py`
  里的 `threading.Lock` 只用于同步后端的自我保护，不是线程池，不要当模板照抄。）

### 4.1 Qt 的边界（`core` 为什么禁止 import Qt）

这条规则的目的**不是**"让代码能在没装 Qt 的机器上跑" —— `PySide6` 是本项目的硬依赖
（见 `pyproject.toml`），那个说法不成立。真正的目的是两条：

1. **可测性**：`core` 的测试不需要创建 `QApplication`、不初始化平台插件、不需要显示器
   或事件循环，随时都能直接跑（现状即如此：`tests/test_core.py` 一个 Qt 实例都不建）。
2. **规则必须可机械判定**："允许 QtCore 但不许 QtWidgets"这类规则会一路滑坡 —— 先加
   `QTimer`，再加 `QObject` 信号，最后 `core` 就绑上了事件循环与线程亲和性。而
   "不 import Qt"一条 grep 就能验，没有解释空间。

**需要平台相关能力（配置目录、数据目录、时区等）时，一律由上层解析后以参数注入，
禁止为此 import Qt。** 现有范例是 `core/cache.py::AudioCache(root=None)`：`core` 只认
`Path`，平台目录由调用方决定，测试因此可以注入沙箱目录。

**点名两条最容易踩的：**

- **`QSettings` 一律不进 `core`**。它在 Windows 上默认走 `NativeFormat`，落在注册表
  （实测 `\HKEY_CURRENT_USER\Software\<Org>\<App>`）：用户看不见、删不掉、无法备份，
  测试也没法像注入目录那样沙箱化。确要跨平台配置，就写成纯 JSON + `Path` 注入 ——
  `json` 是标准库，不新增依赖，与"能用 Qt 自带的就不加包"并不冲突。
- **`QStandardPaths` 也进不了 `core`**。实测在没有 `QCoreApplication`（或未设置
  `applicationName`）时，它只返回泛化目录（如 `AppData/Local`），拿不到带应用名的正确
  路径；它天然依赖一个已初始化的 app 实例。真要它，就让 `ui` 解析好再注入。

允许与禁止的分界（现状经实测核对）：

| 分层 | 能否 import Qt | 现状 |
|---|---|---|
| `core/` | **禁止** | 0 处引用 |
| `api/` | 顶层禁止（`parse_*` 必须零 Qt） | 0 处引用；`api/bilibili.py` 对 Qt 后端的**延迟导入**是唯一例外，只为让本模块在无 Qt 环境下也能 import |
| `net/` | 允许（`QtCore` / `QtNetwork`） | 仅 `client.py` 引用；`base.py` 与 `urllib_client.py` 保持零 Qt |
| `audio/` | 允许（`QtCore` / `QtMultimedia`） | `player.py` 与 `playback.py`（`QObject` 信号）引用；`resolver.py` 与 `__init__.py` 保持零 Qt |
| `ui/` | 允许（不限） | 唯一可以创建 `QApplication`、使用 `QSettings` / `QStandardPaths` 的地方；`scripts/` 下的独立辅助脚本例外（见 `scripts/_helpers.py::ensure_app`） |

> 这条属于第 4 节的规则，**不在**第 0 节的三条红线之内。真要放宽，按第 8 节走：说明
> 违反哪一条、为什么必须、影响是什么，并在交付说明里标注 —— 但请先确认上面那两条收益
> 确实可以放弃。

---

## 5. Qt 与 B站接口的已知坑（改相关文件前必读）

README 的「B站接口实测笔记」有完整说明，以下是**编写代码时必须遵守**的部分：

1. **`Referer: https://www.bilibili.com/` 是硬性要求**，缺了音频 CDN 直接 403；
   下载音频时还要**摘掉 `Origin` 头**（会被按 CORS 处理并被拒）。
2. **gzip 必须自己解压**：urllib 与 QNAM 都不会自动解压。一律走 `core/headers.py`
   提供的 `decompress_all` / 增量解码，禁止自己手写 zlib。
3. **HTTP 412/429 是间歇性风控**：保持主动限速、会话预热、指数退避。禁止删掉或调小
   限速参数来"提升速度"。
4. **QNAM 非线程安全**：`QtNetworkClient` 只能在创建它的线程里使用；它的
   `warm_up()` 是异步的，业务请求必须排队等预热完成（`_warmup_pending` + 定时器推迟，
   **不要用 `sleep`**）。
5. **必须用 `QApplication` 而不是 `QCoreApplication`**，且**先导入 `QtWidgets` 再导入
   `QtCore`**，否则平台插件未初始化会导致 Qt 网络崩溃。见 `scripts/_helpers.py::ensure_app`。
6. **多P语义**：展示用分P `duration`，播放用分P `cid`，缓存键含 `cid`。
7. **缓存写入必须原子**：先写 `.part` 再 `os.replace`，中途失败必须 `abort()`。
8. **异步状态机里禁止回调覆盖已切走的任务**：所有阶段回调前先做 `_alive(state)` 判断
   （见 `audio/resolver.py`），避免旧请求回来时污染新状态。
9. **面向用户的文字一律中文**：界面文案、错误提示、状态文本都用中文；异常消息可含英文
   技术标识（URL、字段名、状态码）。

---

## 6. 测试规范

- 测试框架是**标准库 `unittest`**，禁止引入 pytest 及其他测试库（保持零测试依赖）。
- 单元测试**禁止触网**：接口解析用手写样本（见 `tests/test_api_parsing.py`）。
- **纯逻辑新增或行为变更 → 必须补对应单测**；修 bug 必须留下能复现该 bug 的用例。
- 测试文件命名 `tests/test_<模块>.py`；测试类与方法 docstring 用中文说明**在验证什么**。
- 网络层契约改动要同步更新 `tests/test_backend_contract.py`，保证两种后端仍一致。
- **需要临时目录的新增测试一律用"可注入路径"**（例如给被测对象传 `Path`，形如
  `AudioCache(root=tmp_path)`），不要用 `tempfile.TemporaryDirectory()` —— 前者在 DSH
  沙箱里也能跑，也不依赖目录清理权限。**既有**依赖 `tempfile` 的用例不要为环境去改
  （原因见第 7.1 节）。

| 目的 | 命令（在仓库根目录执行） |
|---|---|
| 安装/同步依赖 | `uv sync` |
| 单元测试（不触网，最常用） | `uv run python -m unittest discover -s tests -v` |
| 端到端冒烟（触网） | `uv run python scripts/smoke_test.py` |
| 两种后端各跑一遍 | `uv run python scripts/smoke_test.py --both` |
| 只验数据链路 | `uv run python scripts/smoke_test.py --no-play` |
| 接口可用性探测 | `uv run python scripts/probe_api.py` |
| 启动应用 | `uv run bilimusic` |

---

## 7. 验证与交付 — 验收标准

**任何改动了 `.py` 的任务，只有满足以下全部条件才算完成：**

- [ ] `uv run python -m unittest discover -s tests -v` **全绿**（附上真实结果，不许编造）；
  在 DSH 沙箱里 `uv` 本身起不来，改用 venv 直调，见第 7.1 节
- [ ] 本次新增/修改的每个模块、类、函数、方法都有**中文 docstring**（见第 2 节）
- [ ] 关键逻辑有中文「为什么」注释；无过时注释、无调试残留
- [ ] 公开接口有完整类型注解
- [ ] 分层依赖方向未被破坏（第 4 节）
- [ ] 未引入第 1.4 节的跑偏功能、未擅自增删依赖
- [ ] 涉及网络 / Qt / 播放的改动：已跑 `scripts/smoke_test.py`，或**明确说明为何未跑**

**硬性禁令：**

- 测试失败**禁止**说"应该没问题""逻辑上是对的"——要么修到绿，要么如实报告失败原因。
- **禁止**为了让测试通过而删测试、跳过测试、放宽断言。
- **禁止**用 `--no-play` / 注释掉代码的方式绕过验证而不说明。
- 只改文档（`*.md`）时不强制跑测试。

### 7.1 环境陷阱：沙箱可能挡住 `%TEMP%`（先排查环境，再怀疑代码）

在 DSH 的 `workspace-write` 沙箱下，Python 进程**创建** `%TEMP%` 下的临时文件可用，
但**读写/删除已存在的临时目录会被拒绝**（`PermissionError: [WinError 5]`，
并伴随 `[sandbox: file access denied]`）。这会同时打掉 `uv`（写不了 uv 缓存）和
`tests/test_core.py` 中所有依赖 `tempfile.TemporaryDirectory()` 的用例。
（2026-09 实测：19 个用例因此报错，报错点全部落在 `tempfile._resetperms` 的 `chmod` 上。
用例总数会随开发增长，**以实际输出为准**，不要照抄某个数字。）

判定方法：看报错里有没有 `[sandbox: file access denied ...]` 或 "拒绝访问 / os error 5"。

- 这是**环境限制，不是代码缺陷**，不要为此改测试、改 `core/cache.py` 或删用例。
- **`uv run ...` 在沙箱里根本起不来**（`Failed to initialize cache at ...\uv\cache`，
  cause 是 `failed to open file ...\sdists-v9\.git: 拒绝访问`）。所以第 6 节与第 7 节
  里的 `uv run` 命令在此环境下要改成 venv 直调：
  `.venv\Scripts\python.exe -m unittest discover -s tests -v`（Windows）。
- **把 `TEMP` / `TMP` 重定向到工作区内的目录并不能绕过**（实测仍报同一个
  `PermissionError`），不要在这上面反复尝试。
- 可先跑不依赖临时目录的模块确认主链路健康：
  `.venv\Scripts\python.exe -m unittest tests.test_api_parsing tests.test_backend_contract tests.test_icons`
  （2026-09 实测：`Ran 49 tests ... OK`）
- 沙箱拒绝删除而残留的目录，会让每次 `git status` 刷出一屏 `Permission denied` 警告；
  清理它需要放宽到更宽的沙箱模式。**不要**为了掩盖这类警告去改 `.gitignore`。
- **应用自己的数据目录同样写不进去**（2026-09 实测）：`workspace-write` 下冒烟脚本走到
  下载步会报 `PermissionError: [Errno 13]` 指向
  `%LOCALAPPDATA%\BiliMusic\Cache\*.m4a.part`，并被标成 `[sandbox: file access denied]`。
  这是**环境限制**，不要为此改 `core/cache.py`；跑 `scripts/smoke_test.py` 需要放宽沙箱。
- `scripts/smoke_test.py` 是触网的，结果受 B站风控与时段影响：实测同一个 Qt 后端第一次
  报 `NetworkError: No credentials (HTTP 0)`、紧接着重跑就全 PASS。**不要**因为一次
  `HTTP 0` 或一次 412 就去改网络层 —— 先重跑，再怀疑代码。
- **这些失败完全取决于当前的沙箱模式**：同一套测试在 `danger-full-access` 下实测
  `Ran 210 tests ... OK`（全绿，含 `test_core` 那 19 个）。所以看到成批
  `PermissionError` 时，先确认当前沙箱模式，而不是去改代码或改测试。
- 交付说明里必须**如实标注**这类失败是沙箱导致的，并说明完整测试尚未验证，
  **不许声称"全部通过"**。

**交付时必须在回复里写清**：改了哪些文件、跑了什么命令、真实结果如何、
哪些没验证到。

---

## 8. 规则冲突与豁免流程

- 用户当轮的**明确指令优先于本文件**，但不得违反第 0 节三条红线。
- 确有必要放宽某条规则时：**必须先说明违反哪一条、为什么必须违反、影响是什么**，
  并在交付说明里再次标注；**禁止悄悄放宽**。
- 不确定是否跑偏时，**先问用户**，不要自行扩范围。
- 发现本文件与代码现状不一致时：如实指出，并**优先按本文件修正代码**；若确认是本文件
  过时，提出修改建议，由用户决定。

---

## 9. 自检清单（提交前逐条过）

1. 文件顶部有模块 docstring（含设计取舍）吗？
2. 新加的类/函数/方法都有中文 docstring 吗？会抛异常的写了 `Raises` 吗？
3. 反直觉、平台差异、性能取舍的地方写清"为什么"了吗？
4. 有没有空话 docstring、复读代码、过期注释、`print` 残留？
5. 类型注解写全了吗？导入顺序对了吗？
6. 依赖方向对吗？有没有把 Qt（含 `QSettings` / `QStandardPaths`）漏进 `core`？需要平台
   能力时是走参数注入而不是 import 吗？两种后端同步改了吗？
7. 有没有引入 `QThread` / 新依赖 / 第 1.4 节的跑偏功能？
8. 新纯逻辑补单测了吗？跑测试了吗？结果是真实的吗？
