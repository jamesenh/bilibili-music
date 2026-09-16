# BiliMusic 后续功能路线图

> 本文是**规划**,不是规范。编码规范与项目方向一律以 [`AGENTS.md`](../AGENTS.md) 为准;
> 两者冲突时以 `AGENTS.md` 为准,并回来改本文。
>
> 状态:`M1` 待开工;`M2`–`M6` 只登记方向,细节等开工前再定稿。

---

## 0. 已确认的决策(2026-09-12)

| 决策项 | 结论 |
|---|---|
| 下一里程碑 | **M5 账号能力(分阶段)已开工,S0 验证进行中**;M1 已完成、M2 剩 M2.5 |
| AGENTS.md 1.4 跑偏清单(登录 / WBI 签名 / 收藏夹云歌单) | **2026-09-14 起按 M5 分阶段授权执行**(S0 只读验证 → S1 最小闭环 → S2 逐项拍板);写回类账号操作、私密收藏夹、云端歌单同步**仍不碰** |
| 界面策略 | **先把 `ui/` 拆成 widget,再往上堆功能** |

这三条是后续所有排期的前提。要改其中任何一条,先改本表,再动代码。

---

## 1. 基线(实测,不是估计)

> 2026-09-14 更新(一键缓存整个合集 + 下载任务对话框落地后重跑);07 节变更记录里有完整清单。

- **单元测试**:共 `687` 个用例(2026-09-14 新增批量缓存调度、任务表与任务对话框后)。
  - 本轮在 macOS 上实测 `Ran 687 tests ... OK`(全绿)。
  - `workspace-write` 沙箱内的历史结论仍然成立:`uv run ...` 起不来,且 `test_core`
    里依赖 `%TEMP%` 的用例会成批报 `PermissionError`,验证要改用 venv 直调
    `.venv\Scripts\python.exe -m unittest discover -s tests`(见 `AGENTS.md` 7.1)。
  - 上一条基线是 `604` 个用例(2026-09-14,本地库迁移 + 最近播放那一轮)。
- **`scripts/smoke_test.py`(触网 + 需 GUI)**:上一轮的 `HTTP 412` 风控已经过去,
  本轮 `--no-play` 在 qt 后端**全 PASS**(搜索 → 多P详情 → DASH 音轨 → 下载 3.68 MB →
  命中缓存零请求)。真实出声那一步仍未跑(`--no-play` 跳过了播放)。
- **批量缓存的真机链路**:**本轮额外验证过一次**(一次性脚本,跑完即删,只写 `/tmp` 沙箱):
  取一个 200P 合集的前 3 个分P → 串行下完 11.95 MB 用时 3.2s → 缓存索引 3 条(含真实码率)
  → 任务记录落库为 `done` → 同库重建 `Downloader`(模拟重启)显示 `paused` 且不自启动
  → 继续时因全部分P已在本地**零请求**判完成 → 重新入队后暂停:`paused` 且目录里没有
  `.part` 残留。这一步覆盖了"真 playurl + 真 CDN 下载",但**没有**覆盖 200P 长任务、
  中途失败、以及与播放同时进行的并发场景(那两种只能靠替身单测)。
- **2026-09-13 的旧数据**(分P选择器那一轮):共 `415` 个用例;放宽沙箱下
  `Ran 377 tests ... OK`(当时是 377 个),`workspace-write` 内 `errors=19` 全部落在
  `tempfile.TemporaryDirectory()` 上,**不是代码缺陷**。

---

## 2. 三个硬约束(它们决定里程碑顺序)

| 现状 | 对规划的影响 |
|---|---|
| `core/cache.py` 只能按 `(bvid, cid, quality_id, codec)` **盲查**,没有索引文件 | 「本地曲库 / 离线歌单」必须先有 sidecar 索引才能枚举已缓存内容 → **M2.1 必须排在 M2 其余任务之前**(已于 2026-09-14 落地,见 07 节) |
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
| M0 | 工程化前置:首次 `git commit` | 无 | **已完成**(仓库已有提交历史,本节是历史登记) |
| M1 | 播放体验骨架 | M0(建议) | **已完成** |
| M2 | 本地曲库与下载管理 | M2.1 索引格式定稿 | **M2.1–M2.4 / M2.6 已完成**,M2.5 登记 |
| M3 | 内容发现 | 接口 probe 结论 | 登记 |
| M4 | 歌词 | 歌词源选型(需单独拍板) | 登记 |
| M5 | 账号能力(登录 / WBI / 收藏夹当歌单) | **用户显式授权(已获)** | **S1 完成(Cookie 登录 + 收藏夹当歌单);S2 待拍板** |
| M6 | 工程化:打包与日志 | 功能稳定 | **日志已完成(2026-09-15)**;打包与 UI 冒烟登记 |

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

> **进度(2026-09-14)**:**M2.1–M2.4 / M2.6 已完成** —— 缓存索引 + "本地缓存"页(枚举 /
> 过滤 / 离线点播 / 删除单曲 / 清空) + "最近播放"页(播放历史) + 批量缓存
> (一键缓存整个合集、串行下载队列、下载任务对话框)。**只剩 M2.5 断点续传**,细节见下。
>
> **2026-09-14 的存储迁移**:索引从缓存目录里的 `index.json` 换成了配置目录下的
> `library.db`(sqlite,标准库),播放历史与它同库不同表 —— 见 07 节。

- **封面磁盘缓存已在 2026-09-13 提前落地**(见 07 节变更记录):`core/cover_cache.py`
  提供 `size_bytes()` / `clear()`,但**没有接界面**——"本地缓存"页要显示封面占用时直接复用。
- **M2.1 缓存索引**:**已落地**(`core/cache_index.py`;记录存在配置目录下的
  `library.db` 里,同日从与音频同目录的 `index.json` 迁过去,见 07 节),
  记录 `bvid / cid / 标题 / UP主 / 分P序号 / 音质 / codec / 真实码率 / 时长 / 封面URL /
  体积 / 落盘时间`。两处偏离原计划,都在 07 节记了理由:①用**单一存储**而不是
  每个音频配一个 `<key>.json`;②索引写入由 `AudioResolver` 的成功出口负责,而不是下载层。
  索引缺失或损坏时退回盲查(`pick_best_cached`),**不会因此播不出来**;反过来,
  `pick_best_cached` 会**先问索引**,于是连 `fLaC` 这类非常规档位的缓存也能命中。
- **M2.2 本地曲库页**:**已落地**(`ui/widgets/cache_page.py` + 主窗口接线)。
  离线播放的做法是"用索引记录重建一个只含该分P的 `Video`",于是解析器既不会请求详情
  (有 `pages`),也会被缓存命中 —— 全链路**零网络请求**。
  过滤为按曲名 / UP主(本控件内做,不涉及业务判断);删除单曲与清空都**先确认**,
  删不掉(文件正被播放器占用)时会说明原因并保留索引,让用户可以停掉播放再试。
- **M2.3 下载队列**:**已落地**(`audio/downloader.py`),但只做**串行**、并发上限固定为 1
  (风控敏感 —— `core/http.py` 的限速参数**禁止**为提速调小;并发也是用户当轮明确否掉
  的)。单次失败的重试交给网络层已有的指数退避;任务级失败改为**挂起整条队列**并弹窗
  (`FAILED` 不自动重试),由用户决定"继续"还是"移除"。暂停/继续/移除三者都有。
- **M2.4 整合集一键缓存**:**已落地**(搜索结果右键 → 确认框写明"共 N 个,已缓存 M 个" →
  串行缓存剩余分P)。**偏离原计划一处**:刻意**不做总量预估** —— 每个分P的体积只能靠
  它自己的 `playurl` 才知道,想在下第一首之前算出 200P 的总量就得先把 200 次请求跑完
  (几分钟无进度等待)。改为"分P计数 + 已下载字节累计"的真实进度(用户当轮拍板);
  也刻意不做下载速度与剩余时间估算。
- **任务跨重启保留**:落 `library.db` 的 `download_tasks` 表(结构版本 1 → 2,纯新增表),
  未完成的任务重启后一律回到"已暂停",要用户点一次才接着下。
- **M2.5 断点续传**:**仍未开工**,而且本轮已明确**不做**:暂停即 `abort()` 掉半截
  `.part`,恢复时那个分P从头下(用户当轮拍板)。`Range` 依旧未实测 —— 所以第 6 节的
  待验证假设照旧悬着,真要做得先验它。
- **M2.6 最近播放**:**已落地**(`core/history.py` + `ui/widgets/history_page.py`)。
  写入挂在 `audio_ready`(音频真正就绪)上,同一首只留最近一次,上限 200 在
  `config.json` 里可手改。刻意**不做**续播(那是"打开就出声"的产品决策,已单独驳回)、
  不做播放次数统计、不做同步。
- 缓存占用与清理已有现成实现:`AudioCache.size_bytes()` / `clear()`。

### M3 内容发现

- **M3.1 搜索分页/加载更多**:**已落地**(搜索结果滚到接近底部自动取下一页,最多 5 页 ≈ 150 条,
  按 `bvid` 去重;翻页失败时底部出现"加载更多"按钮 —— "滚动触发"是隐式的,失败必须给一个
  看得见、点得动的出口)。原来是"UI 没接",已在 2026-09-13 那一轮补上,这里只是把状态改对。
- **M3.2 音乐区排行榜**:候选接口 `/x/web-interface/ranking/v2?rid=3`,需先 probe。
- **M3.3 合集/系列与 UP主投稿**:**UP主投稿可能与收藏夹同受 WBI 限制**,
  必须先 probe;若确认需要签名,本项并入 M5 或直接搁置。
- **M3.4 搜索历史**:**已落地**(实现与取舍见第 7 节 2026-09-16 的变更记录)。
  存储沿用 2026-09-14 落地的 `core/library_db.py`(再加一张 `search_history` 表即可,
  不必另起一份文件):库里留 50 条,搜索框的历史下拉框最多显示最近 10 条、输入时按
  包含匹配过滤。

### M4 歌词

- 已知约束:匿名调 `/x/player/v2` 时 `subtitles` **恒为空数组**(README 实测结论)。
- 候选源:① 第三方歌词库的 LRC 接口 ② 本地 LRC 文件手动关联 ③ 弹幕提取(质量差,不推荐)。
- 建议先做可插拔抽象 `core/lyrics.py` + 本地 LRC 兜底,再决定要不要接第三方源 ——
  接第三方意味着新域名、新限速对象与合规评估,属于要单独拍板的事。

### M5 账号能力(2026-09-14 授权开工,分三阶段)

> **授权范围**:登录 / Cookie 导入 / 扫码 / WBI 签名 / 收藏夹**读取**。
> **不在范围内**:写回类账号操作(收藏 / 取消收藏 / 增删收藏夹)、私密收藏夹、
> 云端歌单同步 —— 要做得单独拍板。
> **凭据纪律**:见 `AGENTS.md` 第 5 节第 10 条(禁止把 `SESSDATA` / `bili_jct` /
> `refresh_token` 写进日志、测试样本、提交或调试输出)。

#### S0 只读验证(进行中,`scripts/probe_login.py`)

目的:**在写任何功能代码之前**,用一个真实账号把下列未知量一次性问清楚。脚本**只读、
不落盘、不打印凭据**(从 stdin / 环境变量读 Cookie,诊断只报名字与长度)。

| # | 要回答的问题 | 为什么必须先答 |
|---|---|---|
| 1 | 真账号下 playurl 的音频档位是否比匿名更高(`dash.flac` / `dash.dolby` 是否非空) | **决定这个里程碑值不值得做**:社区文档只规范了视频清晰度与大会员的关系,从未把 30216/30232/30280/30250/30251 与登录绑定。若登录换不来音质,登录就只是收藏夹的前置件 |
| 2 | 登录态下收藏夹四个读取接口不带 `w_rid` 能否稳定拿到 `data` | 决定要不要先写 `core/wbi.py`(文档口径:不需要;但 `-352` 的定义把"UA 或 wbi 参数不合法"并列,且原 issue 已 404,无法取证) |
| 3 | `medias[]` 真实形态:`type` 分布、`page>1` 占比、失效条目(`attr=1/9`)是否被过滤、`ps=20` 是否真是上限、条目**确实没有 `cid`** | 决定"收藏夹当歌单"要为每个条目补多少次 `view` / `pagelist`(请求量 ×N 直接等于风控风险) |
| 4 | 扫码成功那一跳:`Set-Cookie` 与 `data.url` 的 query 是否始终一致、哪些字段可省、"取消扫码"返回什么内层码 | 决定 S1 的 session 数据模型 |
| 5 | `cookie/info` 何时翻成 `refresh:true`;`correspond/1/<hex>` 的 HTML 是否仍是 `div#1-name` | 决定"登录一次能撑多久",也就决定要不要做那套脆弱的保鲜链路 |
| 6 | 登录态下的风控节奏:现有 `0.8s` 限速够不够、是否 `-352`/`-412`、captcha 是否可解 | 匿名风控是 IP 级可重试;**账号级风控的代价完全不同**,量出来才敢定限速 |

**脚本覆盖率(2026-09-14)**:`scripts/probe_login.py` 覆盖第 1/2/3/6 问与第 5 问的前半
(`cookie/info` 的 `refresh` 标志);**第 4 问与第 5 问的后半它不覆盖** —— 前者要真扫一次码
(放 S1 首步顺带完成),后者要抓一次 `correspond` 页 HTML(属 S2)。脚本自己在结论清单里
也会把这几个"本轮未覆盖"项列出来,免得把没验当成验过。

**S0 结果(2026-09-14,真账号/大会员,两轮,结论已回写 README)**:

| # | 状态 | 结论 |
|---|---|---|
| 1 音质收益 | **非阻塞:5 条样本无增益** | 匿名与登录都是 `[30216, 30232, 30280]`、flac/dolby 全空 —— **匿名本来就已拿到 30280(常规最高档)**;会员档位(30250/30251)取决于稿件本身有没有那条音轨,这 5 条投稿都没有。登录是收藏夹的前置件,M5 的价值不靠它;要不要支持会员档位归 S2,且先得找一条真有 Hi-Res/杜比的视频来验 |
| 2 要不要 WBI | **已答:不需要** | 16 个收藏夹的列表与内容全程不带签名都拿到 `code=0` ⇒ **`core/wbi.py` 不做** |
| 3 `medias[]` 形态 | **已答** | 条目**没有 `cid`**(只有 `page` 分P数);`ps=20` 足额、`has_more=True`;20 条全是 `type=2`;**失效条目占 30%**(`attr≠0` 6/20) |
| 4 扫码凭据形态 | **未覆盖,顺延 S1 首步** | 要真扫一次码 |
| 5 保鲜时机 | **前半已答** | 两轮 `cookie/info.refresh=False`;后半(`correspond` 页结构)属 S2 |
| 6 风控节奏 | **已答(小批量)** | 登录臂 14 次 + 匿名臂 10 次请求,**风控码 0 次**;`0.8s` 限速在登录态下够用 |

**两轮里新收口的一条**:失效条目**靠条目自带的 `attr` 就能认**(`attr=1`/`9` 的条目,
`pagelist` 返回 `-404`、`view` 返回 `-404` 或 `62002 稿件不可见`;`attr=0` 的两边都成功)
⇒ **S1 过滤失效条目零额外请求**。

**退出条件**:上表 6 项都有明确数字或布尔结论,并回写 README「B站接口实测笔记」。
**否决点**:第 2 项**已排除**(不需要签名,S1 不必先插 `core/wbi.py`);第 1 项降级为
非阻塞(它不再决定 M5 做不做,只决定 S2 要不要做会员音质)。

#### S1 最小闭环(进行中:S1a 已完成,S1b 待做)

**S1a 数据层(2026-09-14 完成)**

- [x] `core/session.py`:`Session` / `SessionStore` + `parse_cookie_header` /
      `session_from_cookies`(纯逻辑,**不 import Qt**,路径可注入;缺 `SESSDATA` 一律
      当"没有凭据")。
- [x] **凭据落盘形态已拍板:明文 JSON**(用户 2026-09-14 明确选择,非默认)。
      配套:`save()` 在 POSIX 下把权限设成 `0o600`(且在 `os.replace` 之前设,不留宽松
      窗口);登出走 `clear()`;`AGENTS.md` §5.10 已同步登记这三条要求。
- [x] `net/`:两个后端各加 `session_cookies()` / `set_session_cookies()`,并纳入
      `tests/test_backend_contract.py` 的静态契约与方法签名比对。
      **偏离原计划一处**:原计划用 QNAM 的 `toRawForm()` / `parseCookies()` 做往返,
      实做改成"名字→取值"字典 + 固定 `.bilibili.com` / `/` / `secure`。理由:
      `http.cookiejar` 没有 `toRawForm` 的等价物,手工拼 raw 串会与 Qt 的格式产生细微
      差异,而"两个后端必须一致"是硬约束;而 expires 本来就**刻意不存**(过期由服务端
      说了算,本地记错只会把有效凭据误判成失效) —— 2026-09-14 实测过 Qt 那套往返保真,
      记在 README 里备查。
- [x] `api/bilibili.py`:`AccountInfo` / `FavFolder` / `FavItem` / `FavPage` +
      `parse_nav` / `parse_fav_folders` / `parse_fav_page` + `fetch_nav` /
      `fetch_fav_folders` / `fetch_fav_page`。**登录态一律看 `nav.isLogin`**;
      收藏夹解析容忍 `data=null`(匿名实测就是这个形态)。
- [x] 单测:新增 `tests/test_session.py`(23 个),扩 `tests/test_api_parsing.py`
      (+20 个)与 `tests/test_backend_contract.py`(+4 个)。**测试抓到两个真 bug**:
      ①`vipStatus=0` 被 `or` 短路成 `vipType`,会把过期大会员显示成大会员;
      ②非视频条目(`type=12` 音频)被误判成脏数据丢弃(改成保留并标成不可播)。

**S1b 界面层(2026-09-14 完成)**

- [x] `ui/widgets/sidebar.py`:**歌单变成动态的** —— ``set_playlists()`` 整组替换,信号改成
      带 ``media_id``(名字可以重复、实测还有空标题的夹子,只有 id 是唯一键);删掉了那五个
      写死的假歌单;新增账号按钮(文案随登录态变)与"登录后显示收藏夹"空态提示。
- [x] `ui/widgets/account_dialog.py`(新增):粘贴 Cookie(输入框遮蔽取值)、**明示凭据保存
      路径**、已登录时显示昵称 + 一键登出。它是"哑"的:只收集/展示,校验与落盘在 MainWindow。
- [x] `ui/widgets/fav_page.py`(新增):收藏夹内容页。过滤、显式「加载更多」翻页、
      失效条目**标灰且点不动**(改发原因文本)、私密标记在标题行、按 ``bvid`` 高亮当前播放。
- [x] `ui/widgets/track_list.py`:`TrackRow.dimmed` + ``is_row_dimmed()``。刻意**只加外观**,
      "点了要不要有反应"由页面决定;取消高亮时弱化行恢复弱化色而不是默认色。
- [x] `ui/main_window.py`:登录 / 启动恢复 / 登出 / 收藏夹列表 / 收藏夹内容分页 / 播放接线。
      几条纪律写进了注释:**校验通过才落盘**(粘错不该覆盖原来好用的凭据)、
      **网络失败不删凭据**("问不到"≠"失效")、登出要**清 jar + 强制补一次匿名预热**、
      切收藏夹用 ``_fav_token`` 让迟到的旧响应作废。
- [x] `net/`:两个后端再补 ``clear_session_cookies()`` —— 只删文件不动 jar 的话,SESSDATA
      还在内存里,用户以为登出了其实没有。
- [x] 单测:新增 `tests/test_fav_page.py`(18 个)与 `tests/test_account_dialog.py`(10 个),
      扩 `tests/test_ui_widgets.py`(侧栏动态歌单 / 账号按钮)与
      `tests/test_ui_wiring.py`(新增 `TestAccountWiring` 11 个:登录顺序、启动恢复、
      网络失败保凭据、登出、翻页追加、**迟到响应不污染新收藏夹**、失效条目不请求详情)。
      **测试抓到 1 个真 bug**:``FavPage`` 重绘时没刷新页脚,导致"加载失败"的提示在成功
      加载后仍然挂着。

#### S2 增强(逐项单独拍板)

- **收藏夹列表的本地操作(2026-09-15 完成)**:侧栏「我的歌单」加了「刷新」与「显示/隐藏」
  两个按钮 —— 前者重新取一遍列表(在 B站 网页上新建 / 删除收藏夹之后本机不会自己知道),
  后者打开弹窗挑哪些夹子列进侧栏。**都是只读能力**:隐藏只是本机侧栏的一份备忘
  (``config.json`` 的 ``fav_hidden_ids``),B站 上的收藏夹一个都不会动,弹窗里也这么写了。
- **扫码登录**:需先定二维码方案 —— 2026-09-14 实测 PySide6 6.8.3 **没有** QR 编码能力,
  venv 里也没有 `qrcode` / `segno` / `PIL`,所以要么新增依赖(按第 3 节先问)、要么在
  `core/` 手写 QR 编码器(纯逻辑可单测)。
- **Cookie 保鲜**:三段式(`cookie/info` → `correspond/1/<RSA密文>` 抓 `refresh_csrf` →
  `cookie/refresh` → `confirm/refresh`),**都不需要 WBI**,但要自己实现 RSA-OAEP(SHA-256)
  加密(`cryptography` 不在依赖里)。最脆弱的一环是靠 SSR HTML 抠字段,失败表现是
  "用户某天突然被登出"。
- **WBI 签名**:仅在 S0 第 2 项为"必须"时才做,`core/wbi.py` 走纯函数 + 固定样本单测。
- 收藏夹写回、私密夹、云端歌单同步:**不在本次授权内**。

### M6 工程化

> **进度(2026-09-15)**:**日志已完成** —— 落盘 + 分级别 + 按天轮转(保留 7 天) +
> 自助导出,细节见下。**打包**与 **UI 冒烟脚本**仍未开工。
>
> 已拍板的口径(用户逐项选定):用场景 = 用户自助导出;深度 = 失败 + 关键节点 `INFO`
> + 网络层 `DEBUG`;落点 = 平台日志目录(**与凭据目录物理隔离**);级别 = 默认 `INFO`,
> `DEBUG` 只能手改 `config.json`;轮转 = 按天 × 7 天;入口 = 侧栏底部「打开目录 / 导出」;
> 响应体只在失败时记前 2 KB;崩溃钩子(**暂不装**)。

- **打包**(PyInstaller / Nuitka):注意 FFmpeg 多媒体后端、`ui/resources/` 数据文件、应用图标。
- **[x] 日志(2026-09-15 完成)**:
  * 新增 `core/redact.py`(纯函数脱敏 + handler 层强制生效的 `RedactionFilter`)
    与 `core/logging_setup.py`(`log_root` / `setup_logging` / `export_logs`);
  * **URL 只留 `host + path + query 参数名`,取值全抹**,不判断域名 —— 按域名黑名单识别
    音视频直链漏一个域名就会让带签名的直链落盘,而且漏了看不出来;要看的关键字
    (搜索词 / `bvid` / `cid`)改由 `api` 层以显式变量记;
  * 两种后端共用 `net.base.RequestLogger`,"两边日志字段一致"由构造满足,
    并由 `tests/test_backend_contract.py` 的 5 条用例砸住(含"一次失败只许一条 ERROR");
  * 噪声纪律:封面成功不记、缓存写入绝不逐块记、`parse_*` 一律不打;
  * 已知缺口(有意):不装 `sys.excepthook` / `qInstallMessageHandler` / `faulthandler`,
    闪退与 Qt 告警不落盘。
  * 新增 77 个用例(`test_redact` / `test_logging_setup` / 契约测试 / UI 接线),
    全量 `Ran 899 tests ... OK`。
- **UI 冒烟脚本**:现在只有覆盖网络的 `smoke_test.py`,界面改动没有任何自动化验证手段。

---

## 6. 待验证的技术假设

以下**都还没实测**,开工前不要当结论用:

1. CDN 是否接受 `Range` 请求(决定 M2.5 断点续传做不做)。
   2026-09-14 的批量缓存**绕开了它**:不做字节级续传,暂停/重启都从该分P重下。
2. `/x/web-interface/ranking/v2` 匿名是否返回 `code=0`(决定 M3.2)。
3. UP主投稿接口匿名是否可用(决定 M3.3 的去留)。
4. `QMediaPlayer` 换源后 `seek` 是否可靠(决定 M1.5 的实现方式)。
5. M5 的六个账号链路未知量(登录是否提升音频档位、收藏夹接口要不要 WBI、
   `medias[]` 真实形态、扫码凭据形态、Cookie 保鲜时机、登录态风控节奏)——
   清单见第 5 节 M5 的 S0 表,验证脚本 `scripts/probe_login.py`。

验证手段:给 `scripts/probe_api.py` 加开关,或写临时脚本(账号相关用
`scripts/probe_login.py`,它只读、不落盘、不打印凭据)。
**结论要回写进 README 的「B站接口实测笔记」**,那是本项目沉淀实测结论的地方。

> **2026-09-14 新增风险**:社区权威接口文档仓库 `SocialSisterYi/bilibili-API-collect`
> 已被作者关停(GitHub API 实测 `archived=true`、`default_branch=deprecated`、
> 最后推送 2026-01-30)。此后接口变更**不再有社区权威记录**,只能靠我们自己实测发现 ——
> 这条应当作为"为什么必须把实测结论落进 README"的理由。

---

## 7. 变更记录

- 2026-09-16:**搜索历史(M3.4)+ 搜索框的历史下拉框(用户当轮明确要求)**。
  需求由用户逐条拍定:点搜索框弹下拉、按顺序显示最近搜索词、最多 10 条、点一条自动填充
  并提交搜索、点其它任何地方收起下拉框并取消输入焦点。澄清阶段追问后确定三条细节:
  输入时**按包含匹配过滤**、库里**存 50 条而只显示 10 条**(过滤要能命中第 10 条以前的词)、
  不要求删除单条/上下键选词等额外交互。

  * `core/search_history.py`(新增):`SearchHistory`(`record` / `entries` / `trim` /
    `suggest`)+ 纯函数 `normalize_keyword` / `filter_terms`。分表 `search_history(keyword
    PRIMARY KEY, searched_at)`(**结构版本 2 → 3**;建表语句仍是幂等的
    `CREATE TABLE IF NOT EXISTS`,所以不需要迁移代码),`keyword` 主键 + UPSERT 实现
    "同一个词只留最近一次并提到最前"。规范化只做 `strip()` —— 必须与
    `MainWindow.on_search` 提交时的口径**完全一致**,否则"搜过的词"填回搜索框会变成另一个词。
    `suggest` 先读整份历史(≤50 条)再在内存里按 `casefold()` 做包含匹配:量级极小,
    用 SQL `LIKE` 还得处理通配符转义(关键字里 `%` 很常见)与大小写,不值这个复杂度。
    条数上限是**模块常量**而不是配置项(与 `history_limit` 不同):它只决定下拉框能给多少
    提示,没有值得让用户去手改 `config.json` 的分量。库打不开、行被手改坏(空串/二进制)
    一律当"没有这条",不影响搜索本身 —— 与 `core/history.py` 同一口径。
  * `ui/widgets/search_suggest.py`(新增):`SearchSuggest` + `SearchSuggestRow`。
    下拉框是**中间内容控件下的普通子控件**,不是 `Qt.Popup` 顶层窗口:后者会抓走键盘、
    输入框随即失焦,"边打字边看历史被过滤"就不成立了;代价是"点外面就收起"要自己实现。
    另外,父对象刻意取**内容区**而不是主窗口:直接挂在 `QMainWindow` 下的子控件会被
    `QMainWindowLayout` 接管,浮层的位置就不再由我们说了算。行一律
    `FocusPolicy.NoFocus` —— 否则点在行上会先让输入框失焦、触发"失焦就收起",
    下拉框在 `mouseRelease` 之前就没了,那一行永远收不到点击。
  * 三条**由实测逼出来的**防守(`search_suggest.py` 模块 docstring 有完整记录):
    ①焦点只认用户主动给的那种(`Mouse` / `Tab` / `Backtab` / `Shortcut`),排除
    `ActiveWindowFocusReason` —— 否则切出去再切回来下拉框会自己冒出来,并与"失焦就收起"
    凑成来回闪烁;②尺寸**只在真的变了才 `resize`**,绝不用 `setFixedWidth` /
    `setFixedHeight`(它们每次都 `updateGeometry()`,对父控件意味着一次布局请求,布局又会
    把 `Resize` 发回来,"贴一次 → 布局 → 再贴一次"会自己转起来 —— 用户实测到的
    **CPU 占满 + 界面卡在闪烁上**正是这条链路),且**关着时 `_reposition` 直接返回**;
    ③列表内容没变就不重建行(同一次点击会先来 `MouseButtonPress` 再来 `FocusIn`)。
  * `ui/theme.py`:新增 `QFrame#SearchSuggest` / `QPushButton#SearchSuggestRow` /
    `QLabel#SearchSuggestTerm`(悬停铺底与分P菜单同一套)。`ui/widgets/__init__.py`
    导出新控件;`main_window.py` 负责接线(数据源是 `search_history.suggest`)、
    `on_search` 里记一条历史、并新增 `_on_suggest_activated`(填回输入框 + 立即搜索)。
  * 顺手修掉一个**会让全量单测崩溃**的坑:应用级事件过滤器**不能**由窗口树里的控件自己
    安装(`app.installEventFilter(下拉框)`)—— 应用会长期持有过滤器对象、而控件又在窗口树
    里,窗口销毁时的垃圾回收会让 PySide6 崩在 GC 里(实测 SIGSEGV)。改为全进程唯一的
    `_PressWatcher`(模块级、只持各下拉框的**弱引用**),并在 `MainWindow.closeEvent` 里
    调 `SearchSuggest.detach()` 注销。
  * 验证:`uv run python -m unittest discover -s tests` → `Ran 942 tests ... OK`
    (新增 42 例:`tests/test_search_history.py` 19 例、`tests/test_search_suggest.py`
    16 例、`tests/test_ui_wiring.py` 里 7 例接线)。另外用一次性 cocoa 脚本(跑完即删)
    在**真平台**上量过:点击搜索框后下拉框与搜索框左对齐、紧贴下方、宽度一致,
    "是独立窗口"为假;空转 2.6s 期间 `refresh` 增量为 0、CPU 4%。
    **未覆盖**:真实鼠标的多次点选与拖拽、多屏 / HiDPI、IME 输入法下的焦点行为,
    以及 Windows 平台的真机表现。

- 2026-09-16:**提交搜索即跳回搜索结果页 + 队列面板默认收起(用户当轮明确要求)**。
  两条都是界面行为微调。澄清阶段追问后确认:队列面板是"每次启动都从收起开始、不记忆
  上一次的开/关",而不是把这个状态持久化进 `config.json`(它属于会话内的视图状态,
  与音量 / 播放模式 / 上次分P不是一类东西)。

  * `ui/main_window.py::on_search` 在**提交那一刻**(发请求之前)调
    `_show_page(results_page, "results")`。搜索框长在标题栏、全局可用,原来在"本地缓存" /
    "最近播放" / 收藏夹页上敲回车,结果会加载在一张看不见的页里 —— 用户看到的就是
    "点了搜索没反应"。切片放在提交时而不是结果回来时:结果什么时候回来是网络的事,
    而失败提示(弹窗 + 结果页底部状态行)也长在那一张页上,所以失败路径一并正确。
    点历史词搜索走的是同一条路(`_on_suggest_activated` → `on_search`),因此一并生效。
  * 启动时改为 `_set_queue_visible(False)`:队列面板默认收起(要看时点播放条上那个
    开关)。分P菜单的"不压住队列面板"避让本来就有 `avoid.isVisible()` 判断,收起时
    自然不参与避让,不需要额外分支。
  * 验证:`.venv/bin/python -m unittest discover -s tests` → `Ran 945 tests ... OK`
    (新增 3 例:"在别的页上提交搜索要切回结果页"、"点历史词搜索同样切页"、
    "第 1 页失败也要停在结果页";另有 2 例按新行为改写:
    `test_queue_is_visible_on_start` → `test_queue_is_hidden_on_start`,
    队列开关那条用例的点击顺序倒过来)。`scripts/smoke_test.py --no-play` **已跑通**
    (qt 后端 PASS;本轮只动界面,未碰网络层,故未跑 `--both`)。

- 2026-09-15:**收藏夹列表的两个本地操作(侧栏「刷新」+「显示/隐藏」弹窗)**。

  * 侧栏「我的歌单」分成两行:`我的歌单 + 账号 + 新建` 一行,新加的 **「刷新」**
    与 **「显示/隐藏」** 一行。两个按钮都带图标 + 文字,未登录时禁用(没有登录就没有
    收藏夹可操作)。侧栏宽度 ``SIDEBAR_WIDTH`` 从 **176 加到 200**:176 时连
    "我的歌单 + 昵称 + +" 都要挤掉,再塞两个按钮只能靠截断文字;``title_bar.py`` 的
    ``_LOGO_BOX_WIDTH`` 一并改成**由侧栏宽度推导**(以前是写死的 162,侧栏一改就会让
    搜索框与内容区错位),两条竖线因此仍然对齐。
  * 「刷新」是**真请求**:没有任何缓存可复用,这正是它的意义。因此按风控口径加了
    ``_folders_loading`` 守卫(连点 = 连发)与 ``_folders_token``(登出 / 再刷新时让
    迟到的列表响应作废 —— 否则"刷新途中登出"会被一份旧列表把侧栏重新填满)。
    **刷新失败不清空已有列表**:那多半只是网络抖一下,把用户正看着的东西擦掉换一句报错
    比报错本身更糟。
  * 「显示/隐藏」新增 ``ui/widgets/fav_visibility_dialog.py``:勾选态表示**显示**
    (默认全勾 → 打开时看到的就是侧栏当前的样子,不用在脑子里取反),带全选 / 全不选与
    "已隐藏 N 个"的实时计数,列表可滚动。它是"哑"的:只收集勾选,落盘与重画侧栏在
    ``MainWindow._set_hidden_folders``(与 ``AccountDialog`` 同一纪律)。
  * 隐藏设置落 ``config.json`` 的 ``fav_hidden_ids``(``core/config.py``):新增的
    ``_clean_hidden_ids`` 逐项兜底(去重、升序、丢掉非正数,一个坏元素不连累其余),
    坏文件照旧退回默认值。
    **不按账号分开存**:``media_id`` 在 B站 全局唯一,分表反而会让"换账号登录"丢掉上一份设置。
    隐藏**只影响侧栏列出什么**:正在打开的收藏夹页照旧留着,侧栏一个歌单都不显示时的说明
    文案按"未登录 / 账号下没有夹子 / 全被隐藏"分成三种,免得用户以为自己丢了数据。
  * 新增 36 个用例(`814 - 778`):``tests/test_fav_visibility_dialog.py`` 10 个(含一条
    真跑模态 ``exec()`` 的用例,钉住"确定"不会被当成"取消")、``test_ui_widgets.py`` +9 个
    (两个按钮的信号 / 禁用态 / 刷新中态 / 重建后接回选中态 / 空态文案)、
    ``test_config.py`` +5 个(``fav_hidden_ids`` 的收敛与落盘往返)、``test_ui_wiring.py``
    +12 个(``TestFavFolderActionsWiring``:刷新重取、连点不发第二个请求、登出丢弃迟到
    响应、刷新失败保住列表、隐藏生效并落盘、取消不改任何东西、隐藏正在浏览的夹子不清页面)。
  * **顺带修掉一个既有的测试隔离缺陷**:``test_ui_wiring._rebuild_window`` 与
    ``test_cache_page`` / ``test_history_page`` / ``test_task_dialog`` 里共 7 处
    ``MainWindow(...)`` **漏注入 ``session_store``**,于是窗口会去读**用户真实配置目录**里的
    ``session.json``(``main_window.py`` 的模块 docstring 明令必须注入)。实测表现:本机存在
    真实凭据时,``TestSearchPagingWiring`` 的两个用例被"登录成功但凭据没能保存"的告警搞红
    (沙箱下写不进去);在能写的环境里更糟 —— 测试的假凭据会**覆盖用户真实的 session.json**。
    7 处全部补上沙箱路径。
  * 验证:``.venv\\Scripts\\python.exe -m unittest discover -s tests`` → `Ran 814 tests`,
    **0 failures**,19 errors(全部是 ``test_core`` 里被 ``workspace-write`` 沙箱挡住的
    ``tempfile`` 清理,与本次改动无关,见 ``AGENTS.md`` 7.1),1 skipped。
    **未跑** ``scripts/smoke_test.py``:本次只动界面与配置,没有碰 ``net`` / ``api`` /
    ``audio``,而冒烟脚本在 ``workspace-write`` 下会因写不进 ``%LOCALAPPDATA%\\BiliMusic\\Cache``
    而失败(同一节 7.1 有记录)。

- 2026-09-14:**M5 S1b 界面层落地(Cookie 登录 + 收藏夹当歌单,S1 全部完成)**。

  * 新增三块界面:`account_dialog.py`(粘贴 Cookie,输入框遮蔽取值、明示凭据路径、
    一键登出)、`fav_page.py`(收藏夹内容页:过滤 / 显式翻页 / 失效条目标灰且点不动)、
    以及 `sidebar.py` 的动态歌单(信号改成带 ``media_id``)。**删掉了侧栏那五个写死的
    假歌单** —— "周杰伦""华语经典"点进去只有"功能没做",还会让人以为自己的歌单丢了;
    现在没登录就说"登录后显示收藏夹"。
  * `TrackRow` 新增 ``dimmed``(只加外观)。"失效条目点了要有反应吗"留给页面决定:
    ``FavPage`` 对不能播的行不发播放信号,改发一条中文原因 —— 用户点一下不该换来一个
    必然失败的请求,还得自己猜为什么。实测这类条目占一页的 30%,所以这不是边角情况。
  * 主窗口的四条顺序纪律(都写进了注释,也都有用例钉着):**校验通过才落盘**(粘错的
    Cookie 不该覆盖原来好用的凭据)、**网络失败不删凭据**("问不到"≠"失效")、登出要
    **清 jar + 强制补一次匿名预热**(``warm_up()`` 默认不会重复预热,不补就缺 buvid3)、
    切收藏夹用 ``_fav_token`` 让迟到的旧响应作废(128 条的夹子响应慢,"先点 A 再点 B、
    A 后到"是常见序列)。
  * `net/` 再补一个契约 ``clear_session_cookies()``:只删文件不动 jar 的话,SESSDATA 还在
    内存里,用户以为登出了其实没有。
  * 新增 45 个用例(`778 - 733`):`tests/test_fav_page.py` 18 个、
    `tests/test_account_dialog.py` 10 个、`test_ui_wiring.py` +12 个(``TestAccountWiring``)、
    `test_backend_contract.py` +2 个(``clear_session_cookies``)、`test_ui_widgets.py` +3 个
    (侧栏动态歌单与账号按钮)。
    **测试抓到 1 个真 bug**:``FavPage`` 重绘时没有刷新页脚,"加载失败"的提示在随后成功
    加载一页后仍然挂在状态栏上。
  * 验证:``.venv\\Scripts\\python.exe -m unittest discover -s tests`` → `Ran 778 tests`,
    **0 failures**,5 errors(**与本次改动无关**,见下),1 skipped。
  * **顺带发现一个既有的 Windows 专有测试缺陷**(未修,登记在此):本次沙箱放宽到
    ``danger-full-access`` 后,``test_core`` 里原先被"``%TEMP%`` 权限"挡住的用例往前走了一步,
    暴露出第二层问题 —— ``TestAudioCache`` 的 5 个用例在 ``tempfile.TemporaryDirectory()``
    清理时撞上 ``PermissionError: [WinError 32]``,原因是**测试没有关掉 ``LibraryDb`` 的
    sqlite 连接**,Windows 不允许删除仍被打开的文件(POSIX 允许)。这与之前修掉的
    "同名用例共用 scratch 目录"是同一类问题。按 ``AGENTS.md`` 7.1 的指示
    "既有依赖 tempfile 的用例不要为环境去改",这两处**没有动**,留给用户决定:
    解法是在这些用例里注册一次 ``LibraryDb.close()`` 的 cleanup(在临时目录清理之前跑)。

- 2026-09-14:**M5 S1a 数据层落地(凭据落盘 + 两后端会话契约 + 收藏夹读取接口)**。

  * **凭据落盘形态由用户拍板为明文 JSON**(与 `config.json` 同级),因此按
    `AGENTS.md` §5.10 配套三件事:POSIX 权限 `0o600`(在 `os.replace` 之前设置,不留
    宽松窗口)、界面必须明示路径并给一键登出(属 S1b)、禁令(不进日志/异常/提交)原样有效。
  * 新增 `core/session.py`(`Session` / `SessionStore` / `parse_cookie_header` /
    `session_from_cookies`)。**刻意不存 expires**:过期由服务端判定,本地记一个可能算错
    的过期点,只会把"其实还有效"的凭据误判成失效;登录态一律问 `nav.isLogin`。
  * `net/base.py` 增加契约 `session_cookies()` / `set_session_cookies()` 与共用纯函数
    `is_blank_cookie`,两个后端都实现并纳入 `test_backend_contract.py`。
    **偏离原计划**:改用"名字→取值"字典而不是 Qt 的 `toRawForm()` 往返,理由见 S1 小节。
  * `api/bilibili.py` 新增 `AccountInfo` / `FavFolder` / `FavItem` / `FavPage` 与
    `parse_nav` / `parse_fav_folders` / `parse_fav_page`,以及 `fetch_nav` /
    `fetch_fav_folders` / `fetch_fav_page`;`MAX_FAV_PAGE_SIZE = 20` 与私密位常量
    `PRIVATE_FOLDER_ATTR_BIT = 2` 都按实测口径注释。
  * 新增 45 个用例(`733 - 688`):`tests/test_session.py` 23 个、`test_api_parsing.py`
    +18 个、`test_backend_contract.py` +4 个。**其中两个是测试抓出来的真 bug**:
    ①`parse_nav` 用 `vipStatus or vipType` 取值,`vipStatus=0`(大会员已过期)被短路成
    `vipType=2`,界面会把过期账号显示成大会员 —— 改成显式 `is None` 判断;
    ②收藏夹条目里非视频类型(`type=12` 音频)缺 `bvid` 时被当成脏数据丢弃 —— 改成保留并
    标为不可播(用户得看得见"这里有一条不是视频")。
  * 顺手让 `scripts/probe_login.py` 复用它自己的那份 `parse_cookie_header` 改为复用
    `core/session.py` 的实现,消掉重复。
  * 验证:`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 733 tests`,
    **0 failures**,19 errors 全部是 `test_core` 的 `%TEMP%` 沙箱限制,1 skipped 是
    `test_session` 里那条 POSIX 权限用例(Windows 上没有权限位,按平台跳过)。
  * **未做**:S1b 的界面(登录对话框 / 收藏夹页 / 侧栏真实夹子 / 凭据路径明示与登出),
    以及"扫码登录"与"Cookie 保鲜"(S2)。

- 2026-09-14:**M5 S0 收口(第二轮真账号)+ 失效条目的识别方式定案**。

  * **第 5 问后半与"两条 `-404`"一起收了**:失效条目**靠条目自带的 `attr` 就能认** ——
    `attr=1` 的那条 `pagelist`/`view` 都返回 `-404`;`attr=9` 的那条 `pagelist` 返回
    `-404`、`view` 返回 `62002 稿件不可见`;而 `attr=0` 的正常条目两边都成功。
    ⇒ **S1 过滤失效条目零额外请求**,不用为每条打一次 `view` 去判活。
  * **音质那问改判为非阻塞**:扫了 5 条正常视频,匿名与登录完全一致
    (`[30216, 30232, 30280]`,flac/dolby 全空)。关键认识是**匿名本来就已拿到 30280
    这个常规最高档**,会员档位(30250/30251)取决于**稿件本身有没有那条音轨**。
    所以它不再决定"M5 做不做"(登录是收藏夹的前置件),只决定 S2 要不要做会员音质;
    真要定案得找一条本身就带 Hi-Res/杜比的视频(`probe_login.py --bvid BV...`)。
  * **风控**:两轮共 24 次请求(登录臂 14 + 匿名臂 10),风控码 0 次;业务码只有
    `-404`/`62002`(失效条目)⇒ 现有 `0.8s` 限速在小批量下够用,S1 逐条补 `pagelist`
    时继续保持。
  * **S0 结论**:六问里 3 问定案、1 问前半定案、1 问降级为非阻塞、1 问顺延 S1 首步
    (扫码凭据形态,要真扫一次码)。**S0 到此收口**,可以开 S1。
  * 验证:`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 688 tests`,
    **0 failures**,19 errors 全部是 `test_core` 的 `%TEMP%` 沙箱限制(AGENTS.md 7.1)。
    本轮只改了文档与 `scripts/probe_login.py`,没有触碰库代码。

- 2026-09-14:**M5 S0 第一轮跑完(真账号)+ 脚本两处方法学修正**。

  * **最大未知量问掉了**:收藏夹接口**不需要 WBI 签名** —— 16 个收藏夹的列表与内容全程
    不带 `w_rid` 都拿到 `code=0`。据此**决定不做 `core/wbi.py`**(除非后续遇到别的接口)。
    其余结论(条目没有 `cid`、`ps=20`/`has_more`、失效条目 30%、私密夹可读、
    `cookie/info.refresh=False`、登录态 12 次请求 0 风控码)已回写 README
    「B站接口实测笔记」。S0 六问里 3 问已答、1 问前半已答、2 问待补。
  * **两处方法学修正**(都是第一轮暴露出来的,不改就会得出错误结论):

    1. `check_audio_quality` 原来只对比**一条**视频,得到"登录前后档位完全相同" ——
       但这**推不出"登录无收益"**:那条视频本身没有 Hi-Res/杜比音轨,登录前后当然一样。
       改为扫前 N 条正常视频(`--quality-scan`,默认 5),命中增益就停;
       扫完没有只能写"这 N 条样本上没有增益"。
    2. `check_multi_page` 原来把 `pagelist` 的空结果一律记成 FAIL;实测有两条返回
       `-404`。改为再用 `view` 交叉验证一次,把"**条目本身已失效**"(收藏夹里 30% 是
       这类)与"分P接口出问题"分开 —— 这个区分决定 S1 是"跳过失效条目就行"还是"必须换接口"。

  * 顺带把 `warm_up` 的结论从"请求成功"改成"真的拿到了 `buvid` 系 Cookie":实测主页
    **偶尔不下发** `buvid3`,只看 HTTP 成功会把假通过读成健康。
  * **仍未验证**:S0 第 1 问(音质增益)与两条 `-404` 的成因(下一轮即可收口)、
    第 4 问(扫码凭据形态,要真扫一次码)、第 5 问后半(`correspond` 页结构,属 S2)。
  * 验证:`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 688 tests`,
    **0 failures**,19 errors 全部是 `test_core` 的 `%TEMP%` 沙箱限制(见 AGENTS.md 7.1)。
    脚本本身用匿名模式 + 假凭据跑通了两条路径;真账号路径由用户在本机执行。

- 2026-09-14:**M5 账号能力解冻并开工(S0 只读验证;用户当轮明确授权,"一个阶段一个阶段来")**。
  这是一次**规则放宽**,按 `AGENTS.md` 第 8 节先说明再动手:

  * 放宽的是 `AGENTS.md` 第 1.4 节的两条跑偏项(「登录 / Cookie 导入 / 扫码 / 账号体系」
    与「WBI 签名 / 收藏夹云同步」)。**影响面**:账号能力会往 `core` / `net` / `api` /
    `ui` 四层都加东西,并引入"本地保存账号凭据"这一新的安全面。
  * 同轮在 `AGENTS.md` 新增**第 5 节第 10 条账号凭据纪律**(禁止把 `SESSDATA` /
    `bili_jct` / `refresh_token` 写进日志、测试样本、提交或调试输出;落盘形态必须用户
    拍板,不许默认明文),作为这条放宽的配套约束。
  * **授权范围**只到"登录 + 收藏夹**读取**";写回类账号操作、私密收藏夹、云端歌单同步
    仍在跑偏清单里。
  * 切分方式**按风险而不是按功能模块**:登录单独成一个阶段没有可交付价值(它的可见收益
    取决于"登录能否换更高音质",而文档从未把音频档位与登录绑定),所以
    S0 验证 → S1 最小闭环(登录与收藏夹一起交付) → S2 增强逐项拍板。
  * 本轮落地内容只有三样:①本文件 M5 章节改写为分阶段计划 + S0 问题表;②
    `scripts/probe_login.py`(只读验证脚本,从 stdin / 环境变量读 Cookie,不落盘、
    不打印凭据);③README 回写 2026-09-14 的匿名实测结论并**纠正**"收藏夹需 SESSDATA +
    WBI 签名"这句旧记载(文档口径与社区实现都不要求 WBI,但真账号结论待 S0)。
  * **尚未验证**:S0 需要用户在真账号下运行脚本才能出结论,本轮只完成了脚本与文档。
    脚本的匿名模式已自测(见交付说明),带凭据路径**未实测**。
  * **顺手修了一个 Windows 专有的测试隔离缺陷**(原本会让全量测试必红):
    `tests/_scratch/<用例名>` 这个约定**跨模块重名时会撞车** —— 实测全仓库有 28 处同名
    用例,其中 `test_download_task` 与 `test_downloader` 都有
    `test_clear_finished_keeps_unfinished`。Windows 上删不掉仍被打开的 `library.db`
    (`shutil.rmtree(..., ignore_errors=True)` 静默失败),后跑的用例就继承了前一个用例
    留在库里的任务,`active_count()` 因此多出两条 ⇒ **全量跑必红、单模块跑却绿**;
    POSIX 允许删除打开的文件,所以 macOS 上的 687 全绿基线看不出这条。修法:这两个模块的
    `_SCRATCH` 改为带模块名(`... / "_scratch" / Path(__file__).stem`)。
    **其余 10 个模块存在同样隐患但当前不致病**,登记在此,等哪天真的红了再一起按同一模式改。
  * **本轮验证**:`.venv\Scripts\python.exe -m unittest discover -s tests` →
    `Ran 688 tests`,**0 failures**,19 errors 全部落在 `test_core` 的 `tempfile` 用例上
    (`PermissionError: [WinError 5]`,指向 `%TEMP%\dsh-*`),即 AGENTS.md 7.1 记的沙箱限制,
    不是代码缺陷。**因此"全绿"尚未取得**:需在放宽松的沙箱下重跑一遍才能确认那 19 个。

- 2026-09-14:**一键缓存整个合集 + 串行下载队列 + 下载任务对话框(路线图 M2.3 / M2.4,用户当轮明确要求)**。
  用户当轮拍板的范围与取舍(逐条问过,不是猜的):

  * **任务跨重启保留**(落 `library.db` 新表 `download_tasks`,结构版本 1 → 2)。
  * **暂停 = 丢弃半截 `.part`**,恢复时该分P从头下 —— **不做 `Range` 字节级续传**
    (音频 CDN 是否接受 `Range` 未实测,M2.5 照旧悬着)。
  * **音质固定"自动"**(复用解析器"可用档位取最高"的行为),不做右键选档位。
  * **入口只在搜索结果列表的右键菜单**(队列面板、缓存页、历史页都不加)。
  * **某个分P失败 → 整个任务挂起 + 弹窗提示**,并且**排队中的任务也一并转为暂停**:
    失败绝大多数是风控,继续把排队的任务一个个发出去只会加深惩罚。
  * **真实进度,不做全量预估**(理由见 M2.4 那条),也不做速度/ETA。
  * **已完成的任务保留**在列表里(带"清空已完成"),未完成的可"移除"(只删任务记录,
    已下好的音频留在磁盘上)。
  * **已缓存的分P跳过**;同一视频已有未完成任务就不再新建(已完成的不拦,相当于"重洗")。
  * 新增 UI:「本地缓存」页头部两个按钮 —— 「下载任务 (N)」(未完成任务数写在按钮上)
    与非模态的任务对话框;「打开缓存目录」用 Qt 自带的 `QDesktopServices`(零新依赖)。

  落地内容与取舍:

  * 新增 `core/download_task.py`:`TaskState`(pending/running/paused/failed/done)+
    `DownloadTask` + `DownloadTaskStore` + 两个纯函数(`task_percent` / `task_progress_text`)。
    **不 import Qt**;`bvid` 是主键,所以"一个视频只有一条任务"由主键而不是应用层保证。
    表里**只记进度数字**(分P计数 / 字节数 / 当前分P),**不记分P级明细**:恢复时"还差哪几P"
    由缓存索引现算(索引才是"哪些歌真的在盘上"的唯一事实来源,再维护一份分P状态迟早打架)。
  * 未知状态串读成 `PAUSED` 而不是 `PENDING`:库可以被手改,而一个"认不出来的状态"被当成
    排队中就会在下次调度时自己跑起来。
  * 新增 `audio/downloader.py`:`Downloader`(QObject + 三个信号)。**不复用播放用的
    `AudioResolver`** —— 那是单任务状态机,再调一次 `resolve()` 会取消上一个,共用会导致
    "用户一点播放就把缓存任务掐死"。新管线只下载不播放,与播放共用同一个 `AudioCache`
    (原子写入)与同一个限速器。
  * 调度是"任务串行 + 分P串行",同一时刻只有一个请求在飞(`AGENTS.md` 第 6 节的风控纪律)。
    进度用**分P个数**画进度条(按字节画会卡在 99%,最后几首往往是长曲)。
  * **正在播放的那一P会被推迟一轮**:播放器正在把同一首写进同一个缓存文件,两条管线同时写
    一个 `.part` 会写出坏文件。推迟到本轮末尾再试一次(只重试一轮,免得和播放僵住);仍然在播
    就在任务结束时如实说"还有 N 个未缓存"。
  * `_pump()` 是唯一的调度循环:`in_flight` 是"让出"信号,`_pumping` 挡住同步回调造成的
    重入 —— 客户端命中详情缓存时回调是同步的,不挡就会为 200P 压出 200 层栈。
  * 状态变化一律走 `DownloadTask.with_state()` 这个统一出口(改内存与落库在同一处,不漏写);
    进度数字则就地改 + 逐分P 落库(不逐字节落库,否则会把磁盘写花)。
  * 重启时 `_load()` 把 `PENDING`/`RUNNING` 一律规整成 `PAUSED`(崩溃留下的 `RUNNING`
    也在这里被就地改正),**不会一开机就自己发几百个请求**。
  * 新增 `ui/widgets/task_dialog.py`:`TaskDialog`(非模态、懒建且只建一个)+ `TaskRow`。
    行内显示标题/状态/"已完成 3 / 共 200 个分P"/当前分P百分比/已下载体积,带暂停·继续·移除;
    顶部是"全部暂停 / 全部继续 / 清空已完成"与"共 N 个任务 · M 个未完成"。
  * `ui/widgets/cache_page.py`:头部加"下载任务"(带未完成数)与"打开缓存目录"两个按钮;
    `ui/theme.py` 新增 `TaskRow` 等四组样式;`ui/main_window.py` 新增右键项
    ("缓存到本地" / "缓存全部 N 个分P",数量未知时不瞎猜)、确认框、失败弹窗
    (模态)、`closeEvent` 里的 `shutdown()` 收尾。
  * **"在本地缓存页显示封面占用"仍未做**(M2.1 登记过的那一条),本轮没碰。
  * 新增 81 个用例(`687 - 606`):`tests/test_download_task.py`(模型 / 持久化 / 容错)、
    `tests/test_downloader.py`(串行、跳过已缓存、失败挂队列、暂停与取消、重启恢复、
    正在播放时的推迟与重试;用"挂着不回调"的替身信道逼出这些**有时序**的行为 ——
    同步替身根本测不出"同一时刻只有一个下载")、`tests/test_task_dialog.py`(控件 + 主窗口接线),
    并在 `test_cache_page.py` / `test_library_db.py` 补了按钮与新表的用例。
  * 验证:`.venv/bin/python -m unittest discover -s tests` → `Ran 687 tests ... OK`。
    另外用一次性离屏脚本渲染了"缓存页头部 / 任务对话框(五种状态)/ 空态"三张图肉眼核对版式
    (脚本跑完即删,未入库)。
  * **真机验证**:`scripts/smoke_test.py --no-play`(qt 后端)全 PASS;
    另用一次性脚本对**真**合集跑了批量缓存的整条链路(搜索 → 详情 → 串行下载 3 个分P
    共 11.95 MB / 3.2s → 真索引与真码率入库 → 任务落库 → 重启后暂停 → 继续时零请求完成 →
    暂停清掉 `.part`)。**未覆盖**:200P 长任务的耗时表现、中途真实失败(风控)时的挂起与弹窗、
    以及与播放同时进行(用户一边听一边下)的真机场景 —— 这三条目前只有替身单测。

- 2026-09-14:**「最近播放」落地 + 缓存索引从 JSON 迁到 sqlite(用户当轮明确要求)**。
  用户当轮拍板的范围:只做"按时间倒序、可点回放的列表"(**不做续播**、不做统计、
  不做同步、不做 UI 设置项)。落地内容与取舍:
  - **规则修订(用户授权 D1)**:`AGENTS.md` 第 1.1 节的技术栈表里"本地存储"一条原本是
    "不引入数据库/ORM",现改为"音频文件缓存 + 本地 sqlite 库(`core/library_db.py`),
    不引入 ORM、不新增第三方数据库包";第 4.1 节补了一句:`sqlite3` 是标准库,
    不在"禁止 import Qt"那条红线之内。这是一次**放宽规则**,按第 8 节已告知用户并获准。
  - 新增 `core/library_db.py`(`LibraryDb`):惰性连接、`PRAGMA journal_mode=WAL` /
    `busy_timeout` / `synchronous=NORMAL`、建表幂等、`user_version` 记结构版本。
    **对外不抛异常**:库开不起来时 `available=False`,`execute` 返回 `None`、
    `query` 返回空列表 —— 缓存索引与播放历史都是"可再生/非关键"数据,
    它们出问题不该让应用起不来,更不该让一次已经成功的播放变成失败。
    (这也是本轮唯一允许"静默失败"的地方,所以两层都有专门的用例钉住。)
  - **库文件的位置(F1)**:`~/Library/Application Support/BiliMusic/library.db`
    (macOS;Windows 是 `%APPDATA%\BiliMusic\`),与 `config.json` 同级。音频仍在缓存目录。
    "清空缓存"只删 `cached_tracks` 与 `.m4a`;"清空历史"只删 `play_history` ——
    两边的隔离都有用例钉住(同库不同表,写错一句就是用户数据丢失)。
  - `core/cache_index.py` 重写持久层(schema + UPSERT),**保留 `CachedTrack` 与 `CacheIndex`
    的方法名**:调用点只有 `AudioCache.index`、`main_window`、`cache_page` 三处,
    这个接缝不动,回归面就小。删掉了 JSON 读写、原子替换与字段白名单那套逻辑;
    "最近缓存的在前"改由表里的 `rowid` 倒序表达(语义与原实现一致)。
    新增 `discard_legacy_index()`,在**建库成功之后**删掉缓存目录里的旧 `index.json`
    (G3:不迁移)。代价已写进 README:索引出现之前缓存的非常规档位(如 `fLaC`)
    会变成僵尸文件(盲查只覆盖三档),需要重新缓存一次才自愈。
  - 新增 `core/history.py`:`HistoryEntry` / `PlayHistory`(record / entries / find /
    remove / clear / trim)+ 纯函数 `history_entry_for`。`(bvid, cid)` 主键 + UPSERT 实现
    "同一首只留最近一次";条数上限每次写入后裁剪。
  - `core/models.py`:新增 `PlayableEntry`(`typing.Protocol`)—— 缓存记录与历史记录都满足
    它,于是 `video_from_entry()`("用记录重建只含该分P的 Video")只有一份实现,
    "从缓存点播"与"从历史点播"走同一条路径。另新增纯函数 `format_relative_time`
    (跨天按**本地日历日**分档,不是"满 24 小时";未来时间戳兜底成"刚刚")。
  - `core/config.py`:新增 `AppConfig.history_limit`(默认 200,封顶 2000,非正/非数字退
    回默认),**不做 UI 入口**,手改 `config.json` 即可。
  - UI:新增 `ui/widgets/history_page.py`(与 `CachePage` 同构:过滤 / 统计 / 清空 /
    双击开播 / 行内"+" / 右键菜单);侧栏新增"最近播放"(新图标 `history.svg`);
    `main_window.py` 新增一页接线,并在 `audio_ready` 里写一条历史(A1:音频真正就绪
    才算"听过",解析失败与快速切歌不留痕迹)。`closeEvent` 里显式关掉库连接。
  - `AudioCache` / `MainWindow` 新增 `db` / `library` 注入参数。**测试必须显式注入沙箱
    路径**:默认值是真实用户的配置目录,漏注入的用例会去动用户的库;为此也把现有
    测试里每一处 `AudioCache(...)` 与 `MainWindow(...)` 都补上了。
  - 新增 `tests/test_library_db.py`(连接/建表/容错)与 `tests/test_history.py`
    (记录/去重/排序/上限/容错/跨表隔离)、
    `tests/test_history_page.py`(控件 + 主窗口接线:包括"从历史点播零请求"用的是
    **真解析器** + 一被调用就失败的客户端);`tests/test_cache_index.py` 重写了容错与
    落盘那批(改为直接拿 sqlite 工具塞坏行),`test_core.py` / `test_config.py`
    补了相对时间与 `history_limit` 的用例。用例总数 510 → 604。
  - 顺手修了两处**跨平台的真问题**:①`_is_plain_file_name` 现在明确拒绝反斜杠
    (POSIX 上 `Path(r"..\..\x.m4a").name` 会原样返回整个串,穿越检查会因平台而失效);
    ②`ui/widgets/placeholder.py` 的模块 docstring 还写着"本地缓存…没有实现"(已过时)。
  - 验证:`.venv/bin/python -m unittest discover -s tests` → `Ran 604 tests ... OK`。
    `scripts/smoke_test.py` **未跑通**:搜索步 `[OK]`,随后在 `/x/web-interface/view`
    上连撞 `HTTP 412`(qt / urllib 两种后端一致,同时刻 `curl` 直连也 412 ⇒ 服务端风控),
    按 7.1 未改网络层。因此**真实下载与播放这一段本轮没有验证到**。
- 2026-09-14:**本地缓存页落地(M2.1 缓存索引 + M2.2 本地曲库页,用户当轮明确要求)**。
  落地内容与取舍:
  - 新增 `core/cache_index.py`:`CachedTrack`(记录)+ `CacheIndex`(读写)+ 两个纯函数
    (`entry_for` 从解析结果造记录、`video_from_entry` 把记录还原成可播放的 `Video`)。
    **不 import Qt**(`core` 红线),路径由 `AudioCache` 注入,所以测试可以指到沙箱目录。
  - **偏离原计划一处**:索引是**单个 `index.json`**(与音频文件同目录),不是每个音频配一个
    `<key>.json`。理由:枚举只读一次盘、增删只写一次盘(原子替换),缓存目录里不会散出
    成百上千个小文件;代价是每新增一首整体重写一次,几百首也只有几十 KB。
  - **偏离原计划第二处**:索引写入放在 `AudioResolver` 的**成功出口**(`_remember`),
    命中缓存那条路径也会调用 —— 那是"索引丢失/老缓存"自愈的唯一机会(否则在索引出现
    之前缓存的歌永远不出现在本地缓存页里)。内容没变时 `remember` 跳过落盘。
  - `pick_best_cached` 改为**索引优先、盲查兜底**:索引能给出真实档位与 codec,
    含 `fLaC` 这类不在 `KNOWN_QUALITIES` 里的缓存;索引读不出来或文件被手删时照旧盲查。
    `CachedHit` 增加 `bandwidth` 字段,`api.bilibili.track_from_cache()` 用它把界面上显示的
    码率从"标称值"换成**真实值**(M1.3 留下的那个取舍,到此收口)。
  - 新增 `ui/widgets/cache_page.py`(`CachePage`):复用 `TrackList` 与 `PlaceholderPage`
    (空态),头部是"标题 + 过滤框 + 占用 + 首数 + 清空缓存";过滤按曲名/UP主在**控件内**做
    (纯字符串匹配,不涉及业务判断),过滤中显示"N / M 首";空态文案区分"还没缓存"与
    "过滤没匹配到"。播放中的那一行会被高亮(与队列面板同一套"上层告知"的接法)。
  - `ui/main_window.py`:侧栏"本地缓存"从占位页改为真页面;新增"双击 = 当前列表变队列并
    离线播"(重建 `Video` 时**带上记录里的分P序号**,否则多P会串到第 1P)、行内 "+" 入队、
    右键菜单(播放 / 下一首播放 / 加入队列 / 从缓存删除 / 在B站打开)、清空(都先确认;
    删不掉时说清原因并保留索引)。切到这一页时才 `reload()` + 剪枝,不适合为看不见的页面
    反复读盘。
  - `ui/theme.py` 增加 `QLineEdit#FilterInput`(与播放条音质下拉同尺寸口径)。
  - `core/models.py` 增加 `format_size()`(纯函数,KB 取整 / MB 一位 / GB 两位);
    `core/cache.py` 的 `clear()` 现在会 `prune()`,**删不掉的文件连同索引记录一起留下** ——
    否则界面上会出现"已清空 0 首"但占用纹丝不动这种没人看得懂的状态。
  - 新增 73 个用例:`tests/test_cache_index.py`(索引读写/容错/路径穿越/与磁盘一致性、
    体积格式化、记录与 `Video` 的双向还原)、`tests/test_cache_page.py`(控件行为 + 主窗口
    接线,其中"离线点播零请求"用的是**真解析器** + 一被调用就失败的客户端替身)、
    `tests/test_resolver.py` 扩到索引优先那条路径。
  - 验证:`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 510 tests ... OK`
    (danger-full-access)。另外用一次性离屏截图脚本肉眼核对了"有内容 / 空态"两种版式
    (脚本跑完即删,未入库)。`scripts/smoke_test.py` **未重跑**(未改 `net/` 的请求路径,
    且沙箱内下载步会因 `%LOCALAPPDATA%` 写不进去而失败),按 AGENTS.md 第 7 节如实标注。
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
