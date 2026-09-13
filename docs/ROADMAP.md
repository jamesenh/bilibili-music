# BiliMusic 后续功能路线图

> 本文是**规划**,不是规范。编码规范与项目方向一律以 [`AGENTS.md`](../AGENTS.md) 为准;
> 两者冲突时以 `AGENTS.md` 为准,并回来改本文。
>
> 状态:`M1` 待开工;`M2`–`M6` 只登记方向,细节等开工前再定稿。

---

## 0. 已确认的决策(2026-09-12)

| 决策项 | 结论 |
|---|---|
| 下一里程碑 | **M1 播放体验骨架** |
| AGENTS.md 1.4 跑偏清单(登录 / WBI 签名 / 收藏夹云歌单) | **不碰,保持匿名** |
| 界面策略 | **先把 `ui/` 拆成 widget,再往上堆功能** |

这三条是后续所有排期的前提。要改其中任何一条,先改本表,再动代码。

---

## 1. 基线(实测,不是估计)

> 2026-09-13 更新(新增分P选择器后重测);07 节变更记录里有这次改版的完整清单。

- **单元测试**:共 `415` 个用例(2026-09-13 新增封面磁盘缓存后)。
  - 放宽沙箱(`danger-full-access`)实测过一次全绿 `Ran 377 tests ... OK`(当时是 377 个);
    本轮新增的 38 个用例**未**在放宽模式下重跑。
  - `workspace-write` 沙箱内本轮:`Ran 415 tests`,`errors=19`,**全部**是
    `PermissionError: [WinError 5]` 落在 `tempfile.TemporaryDirectory()` 上
    (`test_core.py`),与 AGENTS.md 7.1 记录的沙箱挡 `%TEMP%` 一致,**不是代码缺陷**。
  - 沙箱内可绿的命令(排除 `test_core`):
    `.venv\Scripts\python.exe -m unittest tests.test_cover_cache tests.test_cover_loader tests.test_api_parsing tests.test_backend_contract tests.test_config tests.test_icons tests.test_queue tests.test_resolver tests.test_ui_widgets tests.test_ui_wiring`
    → `Ran 306 tests ... OK`。
  - `uv run ...` 在沙箱内直接起不来(`uv` 缓存目录写不进去),验证要改用上面的 venv 直调。
- **仓库尚无任何 commit**,全部文件 untracked(`git log` 报 `does not have any commits yet`)。
- `scripts/smoke_test.py`(触网 + 需 GUI):新增分P选择器后跑过一次(默认 Qt 后端),
  搜索 → 多P → DASH → 下载 → 真实播放全部 `[OK]`,末行 `[PASS] qt 后端单后端链路全部打通`;
  上一次界面改版**未重跑**。要动 `net/` 或 `api/` 时按 AGENTS.md 第 7 节补跑 `--both` 并如实汇报。

---

## 2. 三个硬约束(它们决定里程碑顺序)

| 现状 | 对规划的影响 |
|---|---|
| `core/cache.py` 只能按 `(bvid, cid, quality_id, codec)` **盲查**,没有索引文件 | 「本地曲库 / 离线歌单」必须先有 sidecar 索引才能枚举已缓存内容 → **M2.1 必须排在 M2 其余任务之前** |
| 全项目没有配置持久化:音量、播放模式、上次分P、缓存目录都只活在内存 | 队列"记住播放模式"、续播位置、主题选择都依赖它 → 作为 M1 的基础设施先做 |
| `ui/main_window.py` 是 513 行的 MVP 单文件(其中相当篇幅是 AGENTS.md 强制要求的中文 docstring) | 队列面板/封面/歌词塞不进去 → 已确认先拆 widget,作为 M1 第一步 |

同时,有四处**现成可用**的接口,M1 直接接、不要重写:

- `core/models.py::Playlist` 已写好但全项目无人引用(队列可复用其语义)
- `audio/resolver.py::resolve(quality_id=...)` 已支持指定音质,只差 UI 入口
- `Video.cover_https` + `HttpBackend.get_bytes()`(两种后端都已实现)已具备取封面的能力
- `ui/icons.py::DARK` 与 `palette("dark")` 已就绪,换主题**不需要**改图标源文件

---

## 3. 里程碑总览

| 编号 | 里程碑 | 依赖 | 状态 |
|---|---|---|---|
| M0 | 工程化前置:首次 `git commit` | 无 | 待办(建议开工 M1 前先做,否则重构无回退点) |
| M1 | 播放体验骨架 | M0(建议) | **本阶段** |
| M2 | 本地曲库与下载管理 | M2.1 索引格式定稿 | 登记 |
| M3 | 内容发现 | 接口 probe 结论 | 登记 |
| M4 | 歌词 | 歌词源选型(需单独拍板) | 登记 |
| M5 | 账号能力(WBI / 登录 / 收藏夹) | **用户显式授权** | 冻结 |
| M6 | 工程化:打包与日志 | 功能稳定 | 登记 |

---

## 4. M1 播放体验骨架(详细)

**进度(2026-09)**:**M1 六步全部完成** —— 配置持久化 / 播放队列 / 播放编排 / 界面拆分 /
音质切换 / 封面与深色主题,均已实现并跑通测试(含离屏界面接线用例)。

M1 落地时的取舍与遗留,后续动作前先看这里:

- **队列是"视频级"的**:一个 200P 合集在队列里只占一行,但内部 200 首歌仍然按顺序自动
  播放(分P前进由 `audio/playback.py::_on_track_finished` 处理,排在队列前进之前)。
  要把每个分P展开成独立队列项,得先把"展开后队列会长到几百行"的界面行为想清楚,
  暂列 M2。(手动换分P的入口已经有了:播放条上的分P选择器,见 07 节。)
- **上次播放位置只记不消费**:`config.last_position_ms` 已经在切歌与退出时落盘,但启动时
  不会自动接着放。开机自动出声比较打扰,要不要做、怎么做(自动续播 / 给个"继续上次"
  入口)属于产品决策,开工前先问。
- **队列不跨重启恢复**:与配置无关,是有意的 —— 恢复队列本质是本地曲库问题(M2.1),
  否则要做两遍。

### 目标

把"点一首放一首"升级成"有队列、能切歌、能选音质、看得见封面"的播放器,
同时把界面从单文件拆成可组合的 widget。
**本阶段不新增任何网络接口**,只消费已有的 `view` / `playurl`。

### 4.1 任务拆解

**M1.1 配置持久化 —— `core/config.py`(新建)**

- 用纯 JSON,**禁止 `QSettings`**:`core` 不许 import Qt(AGENTS.md 第 4 节红线)。
- 平台路径:Windows 用 `%APPDATA%`;macOS `~/Library/Application Support/BiliMusic`;
  其余按 XDG `$XDG_CONFIG_HOME`。
  注意这里与 `core/cache.py` **故意不同**:缓存用 `%LOCALAPPDATA%`(不该漫游),
  配置用 `%APPDATA%`(用户设置该跟着漫游)。
- 原子写入:沿用 `cache.py` 的"先写临时文件再 `os.replace`"思路,避免留下半截 JSON。
- 字段:音量、播放模式、主题(`light`/`dark`)、缓存目录、上次播放位置(`bvid` + `cid` + 毫秒)。
- 单测 `tests/test_config.py`:默认值、损坏 JSON 的兜底、原子写入、未知键忽略。

**M1.2 播放队列 —— `core/queue.py`(新建)**

- **放 `core/` 而不是 `audio/`**:排序与播放模式是纯逻辑,放 `core` 才能脱离 Qt 单测。
- `PlayQueue`:队列项、游标、四种模式(顺序 / 列表循环 / 单曲循环 / 随机)。
- 随机模式必须**注入 `random.Random(seed)`**,否则测试无法断言顺序(这是实现时就要定下的形态,
  不能事后补)。
- 接口:`append` / `insert_next` / `remove` / `clear` / `next_item` / `prev_item` / `current_item`。
- **开工时需定稿的开放问题**:队列项存轻量的 `(bvid, page_index)` 引用,还是直接存 `Video` 对象?
  倾向存引用(避免详情对象常驻内存),代价是每次要走 `client.cached_video()` 查询。
- 单测 `tests/test_queue.py`:四种模式、边界(空队列 / 单项 / 末项)、固定种子的随机顺序。

**M1.3 播放编排 —— `audio/playback.py`(新建)**

- 把 `PlayQueue` + `AudioResolver` + `PlayerController` 串起来:
  "决定下一项 → 解析(命中缓存直接播) → 播放 → 自然结束则前进"。
- 这段逻辑现在散在 `ui/main_window.py::_on_track_finished` 里,要整体搬下来 ——
  UI 只转发事件、不写业务逻辑(AGENTS.md 第 4 节)。
- 必须继承 `audio/resolver.py` 的 `_alive(state)` 纪律:切歌后旧解析回调回来**不许**污染新状态。
- **`pick_best_cached` 的接法有个坑(实现前须定)**:它现在只返回 ``Path``,而
  `ResolvedAudio` 必须带上 `AudioTrack` 才能显示音质与码率 —— 只知道文件路径
  是拼不出 `ResolvedAudio` 的。两条路:
  1. 把它改成返回 ``(quality_id, codec, path)``,再按 `api/bilibili.py` 里那张
     码率兜底表造一个 `AudioTrack`。代价:界面显示的码率是**标称值**而非文件真实码率
     (该表本来就是接口没给 `bandwidth` 时的兜底)。
  2. 暂不做预判,继续让 `resolver` 先请求 playurl 再命中缓存。代价:已缓存的歌多花
     一次请求(有限速,约 1 秒)。
  倾向 1,但要接受"标称码率"这一取舍;真正的解法是 M2.1 的缓存索引(把真实音质与
  码率写进 sidecar),到那时再把标称值换掉。

**M1.4 界面拆分 —— `ui/widgets/`(新建目录)**

- `player_bar.py`:`PlayerBar` —— 播放/暂停、上一首/下一首、进度、音量、当前曲目信息、音质下拉。
- `track_list.py`:`TrackList` —— 把搜索结果那张五列表格抽出来,**队列视图复用同一个控件**。
- `main_window.py` 退化为组装 + 菜单/状态栏。**衡量标准是职责而不是行数**:解析、队列、
  播放决策这些业务逻辑必须全部搬走(UI 只做展示与事件转发,见 AGENTS.md 第 4 节);
  行数压不到很低是正常的 —— 按第 2 节,每个方法都要写中文 docstring。
- **图标缺口(已核实)**:`resources/icons/` 现有
  `prev/next/repeat/volume/volume-mute/list/heart/download/...`,**没有 `shuffle`**;
  单曲循环也只有一个 `repeat`、没有"1"变体。需要新增 `shuffle.svg`,并给 `repeat` 加带"1"的变体。
  按现有约定:`viewBox="0 0 24 24"`、可见图元统一 `fill="#000000"` 当单色蒙版
  (不要用 `currentColor`,Qt 的 SVG 引擎不认),染色由 `render_pixmap` 负责。
  `tests/test_icons.py` 有图标可用性断言,新增图标要同步补进去。

**M1.5 音质手动切换**

- `resolver.resolve(quality_id=...)` 已支持,接一个下拉框即可。
- 切换后要**保留播放进度**:同一 `cid` 换音质,需要重新解析下载后手动 `seek` 回原位置。
- 下拉框用的是**固定档位列表**(自动 / 192K / 132K / 64K),不为每首歌单独请求一次
  playurl 去问"这个视频到底有哪些档位" —— 那会让切歌多一次网络往返,而且档位本来就
  时有时无。`resolver` 已处理"指定档位这次没有"的情况(退回最高码率)。
- **不要**顺手往 `resolver.KNOWN_QUALITIES` 里补 Hi-Res(`30251`/`fLaC`)与杜比(`30250`/`ec-3`):
  该常量已被明确标注为"只列常规三档",理由是匿名场景下这两个档位基本拿不到、不会落盘,
  加进去只会让每次盲查缓存多跑两轮多余的 `stat`。真要支持,先确认它们确实会被缓存下来。

**M1.6 封面与深色主题**

- 封面:`Video.cover_https` → `backend.get_bytes()` → `QPixmap.loadFromData`。
  在 `TrackList` 行内与 `PlayerBar` 各显示一处;取图失败要**静默退回 `music.svg` 占位**,
  不能弹窗打断播放。
- 深色主题:`icons.palette("dark")` 已就绪,但**图标换色只管图标** ——
  `main_window.py` 里散落的 `setStyleSheet` 写死了浅色语义,要一并梳理,
  并把主题字段落进 `core/config.py`(M1.1)。

### 4.2 验收标准(AGENTS.md 第 7 节逐条对照)

- [ ] `tests/test_config.py` / `tests/test_queue.py` 新增用例全绿(附真实输出)
- [ ] 新增的模块/类/函数/方法全部有中文 docstring,关键分支有中文「为什么」注释
- [ ] 分层未破:队列与配置在 `core`(不 import Qt),编排在 `audio`,UI 不写业务逻辑
- [ ] 未新增依赖、未引入 `QThread` / `threading`
- [ ] `scripts/smoke_test.py` 至少跑一次(含 `--no-play`),如实汇报失败因素
- [ ] `main_window.py` 里不再有业务逻辑(解析 / 队列 / 播放决策),新 widget 能独立构造

### 4.3 风险

- **深色主题**最容易被低估:图标换色只是第一步,控件样式要逐处过一遍,
  很可能牵出 `main_window.py` 里写死的 `setStyleSheet`。
- **随机模式**不注入种子就无法单测 —— 形态必须在实现时就定好。
- **音质切换保留进度**:不同音质的时长可能不同,`seek` 目标要 clamp 到新时长内。
- 沙箱内 `uv run` 不可用,验证命令改用 `.venv\Scripts\python.exe`(见第 1 节)。

---

## 5. M2–M6 概要(只登记,细节待开工)

### M2 本地曲库与下载管理

- **封面磁盘缓存已在 2026-09-13 提前落地**(见 07 节变更记录):`core/cover_cache.py`
  提供 `size_bytes()` / `clear()`,但**没有接界面**——"本地缓存"页要显示封面占用时直接复用。
- **M2.1 缓存索引**:sidecar JSON(`<key>.json`)记录
  `bvid / cid / 标题 / UP主 / 音质 / 时长 / 封面URL / 落盘时间`,让缓存**可枚举**。
  索引缺失或损坏时退回盲查(`pick_best_cached`),**不能因此播不出来**。
- **M2.2 本地曲库页**:离线播放、按标题/UP主过滤、删除单曲。
- **M2.3 下载队列**:串行或并发上限 1–2(风控敏感 —— `core/http.py` 的限速参数
  **禁止**为提速调小),失败重试、暂停/取消。
- **M2.4 整合集一键缓存**:"缓存整个合集(200P)"要给出总量预估且可中断。
- **M2.5 断点续传**:**先实测 CDN 是否接受 `Range` 请求**;不支持就只做失败重试,
  不要假装支持(见第 6 节待验证假设)。
- 缓存占用与清理已有现成实现:`AudioCache.size_bytes()` / `clear()`。

### M3 内容发现

- **M3.1 搜索分页/加载更多**:`BilibiliClient.search_video(page=...)` 已支持,只是 UI 没接。
- **M3.2 音乐区排行榜**:候选接口 `/x/web-interface/ranking/v2?rid=3`,需先 probe。
- **M3.3 合集/系列与 UP主投稿**:**UP主投稿可能与收藏夹同受 WBI 限制**,
  必须先 probe;若确认需要签名,本项并入 M5 或直接搁置。
- **M3.4 搜索历史**:依赖 M1.1 的配置层。

### M4 歌词

- 已知约束:匿名调 `/x/player/v2` 时 `subtitles` **恒为空数组**(README 实测结论)。
- 候选源:① 第三方歌词库的 LRC 接口 ② 本地 LRC 文件手动关联 ③ 弹幕提取(质量差,不推荐)。
- 建议先做可插拔抽象 `core/lyrics.py` + 本地 LRC 兜底,再决定要不要接第三方源 ——
  接第三方意味着新域名、新限速对象与合规评估,属于要单独拍板的事。

### M5 账号能力(冻结中)

- 路径:`core/wbi.py`(纯函数,固定样本单测)→ Cookie 导入 → 扫码登录 → 收藏夹当歌单。
- **当前已确认不做**。要开工必须由用户显式授权,并**同步修订 AGENTS.md 1.4 清单** ——
  那份清单是"跑偏"的判定依据,不能只在本文件里破例。

### M6 工程化

- **打包**(PyInstaller / Nuitka):注意 FFmpeg 多媒体后端、`ui/resources/` 数据文件、应用图标。
- **日志**:目前只有 `status_label` 与异常文案,没有落盘日志,排障只能靠复现。
- **UI 冒烟脚本**:现在只有覆盖网络的 `smoke_test.py`,界面改动没有任何自动化验证手段。

---

## 6. 待验证的技术假设

以下**都还没实测**,开工前不要当结论用:

1. CDN 是否接受 `Range` 请求(决定 M2.5 断点续传做不做)。
2. `/x/web-interface/ranking/v2` 匿名是否返回 `code=0`(决定 M3.2)。
3. UP主投稿接口匿名是否可用(决定 M3.3 的去留)。
4. `QMediaPlayer` 换源后 `seek` 是否可靠(决定 M1.5 的实现方式)。

验证手段:给 `scripts/probe_api.py` 加开关,或写临时脚本。
**结论要回写进 README 的「B站接口实测笔记」**,那是本项目沉淀实测结论的地方。

---

## 7. 变更记录

- 2026-09-13:**封面新增磁盘缓存(用户当轮明确要求)**。落地内容与取舍:
  - 新增 `core/cover_cache.py`(`CoverCache`,纯逻辑、**不 import Qt**):文件名 =
    URL 的 SHA-1 前 20 位,扩展名按图片魔数推断(`.jpg` / `.webp` / `.png` / `.gif` /
    `.bmp`,认不出退回 `.img`);写入沿用"先 `.part` 再 `os.replace`"的原子纪律;
    默认目录是缓存根下的 `covers/`(与音频文件分开)。`core/__init__.py` 的子模块清单同步补上。
  - **自带 200 MiB 容量上限**:音频是用户主动缓存的,封面却只是"翻列表"的副产品,
    不设上限就是磁盘泄漏。超限按 `mtime` 从旧到新删到 90% 低水位;估算值失真时以扫盘
    得到的真实占用为准(不能照估算值误删刚存的好图)。不用 `atime` 做 LRU:多数文件系统
    把它关了或只在日期变化时更新。
  - `ui/cover_loader.py` 由"内存缓存 → 网络"改成三层:**内存 → 磁盘 → 网络**。磁盘命中
    **同步**返回并回填 `QPixmapCache`(本地读几十 KB 远比一次网络往返便宜,也不必为此起
    线程);解不出图的坏条目当场 `discard`(否则每次加载都白读盘,而"重新下载覆盖"未必
    发生);`clear()` 仍只清排队与在飞请求,**不删磁盘**。磁盘缓存是构造参数,默认不落盘 ——
    测试才不会写到真实用户的缓存目录。
  - `ui/main_window.py` 新增 `cover_cache` 注入参数,三个加载器**共用同一个实例**
    (同一张图在列表与播放条是同一个 URL,谁先取到就落盘,另一个直接命中)。
  - 验证:`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 415 tests`,
    `errors=19`(全是 `test_core` 的 `tempfile` 用例撞上沙箱挡 `%TEMP%`,见第 1 节);
    排除 `test_core` 的 306 个用例 `OK`。新增 38 个用例:`tests/test_cover_cache.py`
    30 个(纯逻辑:后缀推断、0 字节不算命中、原子写入失败不抛、容量上限与清理)、
    `tests/test_cover_loader.py` 7 个(落盘、跨"重启"命中、坏条目自愈、`clear` 不删盘)、
    `tests/test_ui_wiring.py` 1 个(重开窗口直接命中磁盘、不再请求)。
  - 另做了一次**真封面端到端**(一次性脚本,跑完即删):urllib 后端搜到封面(800×450,
    33030 字节)→ 落成 `2276c430c688051be435.jpg` → 清空 `QPixmapCache` + 换新加载器后
    **同步命中、0 次网络请求**。Qt 后端这次搜索步仍报文档里记过的
    `NetworkError: No credentials (HTTP 0)`,故真机验证走的是 urllib 后端(取图走的是
    `HttpBackend.get_bytes`,两种后端同一份实现)。`scripts/smoke_test.py` **未重跑**:
    它不覆盖封面链路(搜索 → playurl → 下载 → 播放),且沙箱内下载步会因
    `%LOCALAPPDATA%` 写不进去而失败。

- 2026-09-13:**播放条新增分P选择器(用户当轮明确要求)**。落地内容与取舍:
  - 新增 `ui/widgets/page_selector.py`(`PageSelector` + `PagePopup` + `PageRow`);
    `PlayerBar` 把选择器排在"分P选择器 + 音质下拉"那一行的音质**之前**、播放控制按钮的右侧。
    它只做展示与事件转发,分P数据由主窗口从编排层读出来推下去。
  - `audio/playback.py` 新增 `PlaybackController.play_page()`:换分P**不改队列**,
    走既有的 `_start_current(page_index=...)` 路径(取消在飞解析 → 按该分P的 `cid` 重新
    解析 → 加载本地新文件并自动播放),缓存键因此天然带上该分P的 `cid`。
    菜单行显示的是分P**自己的**标题与时长(领域铁律),没有用视频级的 duration / cid。
  - 弹出菜单是 `Qt.Popup` 的 `QFrame` 而不是 `QMenu`(一行要放四段内容 + 单选圆点);
    锚在选择器上方、底边紧贴,水平居中,随后按"不越出窗口""不压住右侧播放队列面板"
    向内收 —— 最后一条会挤掉居中,所以由主窗口把队列面板的引用注入播放条。
  - 实测踩到并处理的两处 Qt/平台行为:①窗口首次创建时平台会按边框宽度挪一次客户区
    (离屏平台 2px),所以 `show()` **之前**先 `winId()` 把原生窗口建出来;
    ②QSS 描边占内部尺寸,菜单高度漏算它就会多出一条 2px 的假滚动条。
  - 验证:`.venv\Scripts\python.exe -m unittest discover -s tests -v`
    → `Ran 377 tests ... OK`(新增 39 个用例:播放层换P、控件与菜单、界面接线与版式)。
    `scripts/smoke_test.py`(Qt 后端)**已跑通**:搜索 → 多P → DASH → 下载 → 真实播放
    全部 `[OK]`。本轮未改 `net/`,所以没有跑 `--both`。
- 2026-09-13:**按 UI 设计稿重做版式(用户当轮明确要求)**。落地内容与取舍:
  - 新增自绘标题栏 + **无边框窗口**(`ui/widgets/window_frame.py`、`title_bar.py`),
    边缘缩放交给平台的 `startSystemResize`,不自己算坐标。
  - 新增左侧导航 `sidebar.py`;「发现」「本地缓存」「我的歌单」**没有对应功能**,
    按用户选择做成**说明清楚的占位页**而不是新增页面功能(仍属 M2 / M3 / M5)。
  - 曲目列表改为**封面 + 标题 + 副标题**的富信息行(`track_list.py` 重写),
    行内"+"入队、操作列"⋮"接既有右键菜单。
  - 队列面板不再自带折叠按钮:显示/隐藏改由播放条与侧栏的队列开关负责(两处状态同步)。
  - **主题从"可切换深浅"改为深色单主题**(用户选择):删掉 `AppConfig.theme`、
    `THEMES` / `DEFAULT_THEME` 与 `icons.LIGHT`,主题新增全局样式表(`theme.stylesheet()`)。
    这是一处**功能删减**,要恢复浅色主题需要重新引入上述三处。
  - 封面加载器改为**一次只发一张**的串行队列:`get_bytes` 与 API 共用限速器,
    列表几十行同时甩请求既是请求突发,也会把 playurl 挤到限速窗口后面。
  - 验证:`.venv\Scripts\python.exe -m unittest discover -s tests -v`
    → `Ran 338 tests ... OK`(非沙箱模式;`workspace-write` 下 `test_core` 的 19 个
    临时目录用例仍会因 `%TEMP%` 被拒而报错,见 `AGENTS.md` 7.1)。
    `scripts/smoke_test.py` **未重跑**(本轮只动界面,未碰网络 / 解析 / 缓存)。
- 2026-09-12:初稿。确认下一里程碑为 M1;确认不碰登录/WBI/收藏夹;确认先拆 widget。
