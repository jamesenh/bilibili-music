# BiliMusic

聚合哔哩哔哩音乐视频的桌面音乐客户端。**基于 PySide6 从零自研**,B站的接口与数据获取方式参考了社区现有项目。

当前状态:**MVP 端到端已跑通**(搜索 → 分P → 音轨 → 缓存 → 播放),两种网络后端均验证可互换;单元测试全部不触网,用 `uv run python -m unittest discover -s tests` 可自查。

> [!IMPORTANT]
> **用 AI Agent 改这个仓库前,必须先读 [`AGENTS.md`](AGENTS.md)。**
> 里面是强制的编码规范与项目方向(中文 docstring/注释要求、分层约束、跑偏清单、
> 交付验收标准)。本 README 讲"是什么",`AGENTS.md` 讲"必须怎么写"。

---

## 快速开始

```bash
# 依赖由 uv 管理,Python 固定为 3.11(见 .python-version)
uv sync

# 启动应用
uv run bilimusic
# 或
uv run python -m bilibili_music
```

验证与诊断脚本:

```bash
# 端到端冒烟测试(搜索/详情/音轨/下载/真实播放)
uv run python scripts/smoke_test.py                    # 默认 Qt 后端
uv run python scripts/smoke_test.py --backend urllib    # 换 urllib 后端
uv run python scripts/smoke_test.py --both              # 两种后端各跑一遍
uv run python scripts/smoke_test.py --no-play           # 跳过播放,只验数据链路

# B站接口可用性探测(怀疑接口变动或被限速时先跑这个)
uv run python scripts/probe_api.py

# 单元测试(不触网)
uv run python -m unittest discover -s tests -v
```

---

## 技术选型

| 项 | 选择 | 原因 |
|---|---|---|
| GUI | **PySide6 6.8.3** | 官方 Qt for Python 绑定(LGPL)。6.8 是 Qt 的 LTS 分支,6.8.3 是该分支最终补丁版 |
| Python | **3.11**(`.python-version`) | PySide6 6.8.3 声明 `requires_python = <3.14`,故**不能用 3.14**。3.11 在官方支持区间内且生态成熟 |
| 网络 | **`QNetworkAccessManager`** | Qt 原生异步,信号驱动,零线程。Cookie 由 `QNetworkCookieJar` 管理。详见下方"为什么用 QNAM" |
| 播放 | `QMediaPlayer` + `QAudioOutput` | PySide6 自带,后端为 FFmpeg 7.1,零额外依赖 |
| 包管理 | `uv` | 速度快,自动管理 Python 版本 |

> PySide6 发布的是 `cp310-abi3` 稳定 ABI 轮子,理论上 3.10+ 都能装,但 6.8.3 的元数据**显式排除 3.14**,所以老老实实用 3.11。

### 为什么用 QNAM 而不是 requests/urllib

最初用的是标准库 `urllib`,后来迁移到 `QNetworkAccessManager`。理由是:

1. **原生异步**。`urllib` 是同步阻塞的,为了不卡界面必须自己套一层 `QThread` —— 迁移后整个工作线程层被删掉了
2. **零新增依赖**。QtNetwork 已随 `PySide6-Essentials` 提供
3. **Cookie 自动管理**。`QNetworkCookieJar` 按域保存,不用手写 jar
4. **学习价值**。信号槽驱动的异步是 Qt 应用的核心技能

代价是 `net/` 层依赖 Qt(HTTP 相关的纯逻辑已抽到 `core/headers.py`,仍可脱离 Qt 单测)。为了保留"无 Qt 也能跑"的能力,两种后端都保留了,接口完全一致,可用 `--backend` 切换。

---

## 目录结构

```
src/bilibili_music/
├── core/            与 UI 无关的纯逻辑,可独立单测(不 import Qt)
│   ├── errors.py      异常体系
│   ├── models.py      Video / Page / AudioTrack 数据模型
│   ├── headers.py     请求头构造、gzip 解压(纯函数,无 Qt)
│   ├── http.py        常量与调优参数(退避、超时、限速)
│   ├── cache.py       AudioCache / DownloadSink(原子写入)
│   ├── queue.py       PlayQueue / PlayMode(队列与播放模式)
│   └── config.py      AppConfig / ConfigStore(纯 JSON 落盘)
├── net/             网络后端,两者接口一致可互换
│   ├── base.py        契约(Protocol)、重试策略、限速器、响应校验
│   ├── client.py      QtNetworkClient —— QNAM 实现(默认)
│   └── urllib_client.py  UrllibClient —— 标准库实现(对照/退路)
├── api/
│   └── bilibili.py    接口封装 + 纯解析函数(parse_*)
├── audio/
│   ├── resolver.py    AudioResolver —— 异步解析状态机(无线程)
│   ├── player.py      QMediaPlayer 封装
│   └── playback.py    PlaybackController —— 队列 / 解析 / 播放的编排
└── ui/
    ├── main_window.py 组装与接线(不写业务逻辑)
    ├── theme.py       深色主题:QPalette + 全局样式表
    ├── icons.py       SVG 定位、栅格化与运行时着色
    ├── pixmaps.py     圆角封面、封面占位图、标题栏图标
    ├── cover_loader.py 封面加载(缓存 + 一次只发一张)
    └── widgets/       可单独构造、可单独测的控件
        ├── window_frame.py  FramelessWindow(无边框 + 边缘缩放把手)
        ├── title_bar.py     TitleBar(图标 / 搜索框 / 窗口按钮)
        ├── sidebar.py       Sidebar(导航 + 我的歌单)
        ├── track_list.py    TrackList(搜索结果与队列共用的曲目列表)
        ├── player_bar.py    PlayerBar(传输控件 / 进度 / 分P / 音质 / 音量)
        ├── page_selector.py PageSelector(分P选择器与它的弹出菜单)
        ├── queue_drawer.py  QueueDrawer(右侧播放队列)
        ├── placeholder.py   PlaceholderPage(尚未实现的功能的说明页)
        └── elided_label.py  ElidedLabel(按宽度省略的标签)
```

设计原则:**分层单向依赖**,`core` 不认识 Qt 也不认识 B站,`api` 不碰界面,`ui` 依赖全部。
解析逻辑(`parse_*`)与请求逻辑分离,所以接口响应的解析可以用固定样本单测,不需要网络。

---

## B站接口实测笔记

以下都是**实测踩出来的**,不是抄文档。改动 `core/headers.py`、`net/` 或 `api/bilibili.py` 前建议先读一遍。

### 匿名可用的接口

| 接口 | 用途 | 匿名结果 |
|---|---|---|
| `/x/web-interface/view` | 视频详情、分P列表 | `code=0` |
| `/x/player/playurl` (`fnval=16`) | DASH 音轨 | `code=0`,返回多条独立音频流 |
| `/x/web-interface/search/type` | 视频搜索 | `code=0`,**无需 WBI 签名** |

仍需登录 / WBI 签名的能力(本 MVP 未实现):

- 收藏夹列表与内容(需 `SESSDATA` + WBI 签名 `w_rid`/`wts`)
- CC 字幕:匿名调用 `/x/player/v2` 时 `subtitles` **恒为空数组**

### 坑 1:`Referer` 是硬性要求,`Origin` 反而要摘掉

音频 CDN 有防盗链,缺少 `Referer: https://www.bilibili.com/` 会**直接 403**。

反过来,下载音频时要**摘掉 `Origin` 头** —— 它会让 CDN 按 CORS 处理并可能拒绝请求。

> **注意这条只对音频 CDN 成立。** 封面 CDN(`i0.hdslb.com` / `i1.hdslb.com`)实测
> **不介意** `Origin`:带与不带都返回 `200` 且字节数完全相同(651508 字节的同一张
> jpg)。所以取封面直接用后端的默认 API 头即可,不要照搬"摘 Origin"这条规则
> —— 见 `api/bilibili.py::fetch_cover` 的 docstring。

### 坑 2:必须自己解压 gzip(两个后端都一样)

我们在请求头声明了 `Accept-Encoding: gzip`。**`urllib` 不会自动解压,Qt 也不会** ——
实测 QNAM 的 `readAll()` 拿到的仍是 `1f8b` 开头的压缩字节。漏掉这一步会得到二进制乱码
并在 JSON 解析处报错。

实现见 `core/headers.py`:同时提供一次性解压 `decompress_all` 和流式增量解压
`IncrementalDecoder`(避免大响应全量进内存)。

### 坑 3:风控 412 是间歇性的

同一份请求连续发,可能第 3 次就返回 **HTTP 412**。应对措施(实测有效):

1. **会话预热**:先访问一次 `https://www.bilibili.com/`,拿到 `buvid3` / `b_nut` cookie。
   实测 **B站 API 响应本身不会下发这两个 cookie**,只有主页会 —— 所以预热是必需的。
2. 把 412/429 纳入**指数退避重试**,并在重试前重新预热换一份新鲜 cookie。
3. 请求间保持最小间隔(默认 0.8s)主动限速。

> **Qt 后端实现要点**:`warm_up()` 是异步的,所以业务请求**必须排队等它完成**,
> 否则会赶在带 cookie 的主页响应之前发出。实现方式是 `_warmup_pending` 标志 +
> `_schedule()` 轮询推迟(用定时器,不用 `sleep`)。

### 坑 4:多P视频的 `duration` 是总和

这是最容易写出 bug 的地方:

```
data.duration == sum(page.duration for page in data.pages)
data.cid      == 第 1P 的 cid
```

即 `BV1fx411N7bU`(200P 合集)的 `duration` 是 **50722 秒(14 小时)**,但每首歌只有几分钟。
B站音乐区**到处都是**这种合集("全MV合集""专辑全集"),实际上**每个分P就是一首歌**。

所以:

- 展示单曲时长必须用分P自己的 `duration`
- 播放必须用分P的 `cid`,否则永远只播第一首
- 缓存键必须包含 `cid`,否则不同分P会串歌

### 坑 5:QNAM 非线程安全

实测在子线程复用主线程创建的 `QNetworkAccessManager`,Qt 会打印:

```
QObject: Cannot create children for a parent that is in a different thread.
```

**注意它"看起来成功了"** —— 简单场景能返回数据,复杂场景会随机崩溃。所以
`QtNetworkClient` 必须只在创建它的线程里使用。

### 坑 6:`QCoreApplication` 不够,要用 `QApplication`

用 `QCoreApplication` 且先导入 `QtCore` 时,平台插件不会被初始化,Qt 网络会报
`QSocketNotifier: Socket notifiers cannot be enabled or disabled from another thread`
并可能直接崩溃。**先导入 `QtWidgets` 再导入 `QtCore`,并创建 `QApplication`** 才正常。
见 `scripts/_helpers.py::ensure_app`。

### 音质档位

| `id` | 档位 | codec |
|---|---|---|
| 30216 | 64K | `mp4a.40.5` |
| 30232 | 132K | `mp4a.40.2` |
| 30280 | 192K | `mp4a.40.2` |
| 30250 | 杜比全景声 | `ec-3` |
| 30251 | Hi-Res 无损 | `fLaC` |

杜比与 Hi-Res 在 `dash.flac` / `dash.dolby` 字段里,通常需要大会员。
注意**同一个视频在不同时刻可能只给到部分档位**,不保证 192K 一定存在。

### 为什么缓存的是文件而不是 URL

`playurl` 返回的 CDN 直链**带签名且会过期**(通常几小时)。另外如果让 `QMediaPlayer`
直接播远程 URL,Qt 的媒体请求不经过我们的网络层,**无法附加防盗链必需的 `Referer`**,
实测会被 CDN 拒绝。

因此流程固定为:解析直链 → 流式下载到本地缓存 → 播放本地文件。
顺带获得离线播放能力,并且因为边收边写盘,两小时的合集也不会吃内存。

---

## 已实现的功能

- [x] **自绘标题栏的无边框窗口**:应用图标 + 圆角搜索框 + 粉色搜索按钮 + 最小化/最大化/关闭,
      边缘可拖动缩放(缩放手势交给平台处理,吸附与多屏表现与原生窗口一致)
- [x] **左侧导航栏**:搜索结果页是真的;"发现""本地缓存"与"我的歌单"给出**说明清楚的占位页**
      (功能分别属于路线图 M3 / M2 / M5,还没有实现)
- [x] 关键字搜索视频(**结果列表带封面缩略图**,含标题/UP主/时长/分P数/播放量)
- [x] 自动识别多P合集(**每 P 即一首歌**)
- [x] **播完自动跳到下一个分P**(多P合集时相当于自动下一首)
- [x] **播放条上的分P选择器**:菜单锚在选择器上方列出当前视频的每个分P(编号 / 标题 / 该P自己的时长),
      选中即切到该分P(按该分P的 `cid` 解析与缓存);单P视频或没有当前视频时禁用
- [x] **播放队列**:双击搜索结果=整个结果成为队列并从该行开始播;行内"+"直接入队;
      右键可"下一首播放 / 加入队列"
- [x] **右侧播放队列面板**:待播列表带封面、高亮当前项、双击跳转、右键移除、一键清空;
      显示/隐藏在播放条与侧栏各有一个开关(两处状态始终一致)
- [x] **四种播放模式**:顺序 / 列表循环 / 单曲循环 / 随机(模式会被记住)
- [x] **手动切歌**:上一首 / 下一首 / 播放暂停按钮
- [x] **手动切换音质**(自动 / 192K / 132K / 64K),切换后**保留播放位置**
- [x] 音质自动选择(默认最高码率),界面显示实际音质与码率
- [x] 异步下载,界面全程不阻塞,带进度条
- [x] 音频磁盘缓存(原子写入,二次播放**零请求**直接开播)
- [x] 详情接口内存缓存(减少请求,降低风控风险)
- [x] 播放 / 暂停 / 进度拖拽 / 音量 / 播放结束回调
- [x] **封面显示**(播放条与列表行,内存缓存,失败静默退回占位图);
      列表封面**一次只取一张**,避免把几十个请求一起甩给 CDN
- [x] **深色单主题**:近黑底 + B站粉强调色,`QPalette` 与全局样式表一起上(设计稿即深色,不做浅色变体)
- [x] 音量、播放模式与上次播放位置持久化到配置文件(`%APPDATA%\BiliMusic\config.json`)
- [x] 两种网络后端可互换(Qt / urllib)

## 尚未实现

- [ ] 登录(扫码 / Cookie 导入)与收藏夹同步为歌单
- [ ] WBI 签名(`w_rid` + `wts`),收藏夹等接口的前提
- [ ] 歌词:目前匿名拿不到 CC 字幕;可考虑从视频弹幕或第三方歌词库补齐
- [ ] "发现"(音乐区排行榜)与"本地缓存"(本地曲库)两个页面:侧栏已留入口,点开是占位页
- [ ] 歌单(含"喜欢"按钮):侧栏已留分组,点开是占位页
- [ ] 全屏播放(播放条右下角的按钮当前**禁用**,没有假装能用)
- [ ] 上次播放位置**续播**:配置里已经在记录,但启动时不会自动接着放
      (开机自动出声比较打扰,要不要做得先定;见 `docs/ROADMAP.md`)
- [ ] 队列跨重启恢复(当前只记播放模式与位置,不记住队列内容)
- [ ] 并发下载与断点续传
- [ ] 打包分发(PyInstaller / Nuitka)

> 上面这些的排期、依赖关系与下一步要做什么,见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。
> 该文档还记录了已拍板的决策(当前阶段不碰登录/WBI/收藏夹)与**尚未实测的技术假设**。

---

## 合规提示

本项目通过非官方接口访问 B站数据,处于**灰色地带**:

- 仅供**个人学习与技术研究**使用,请勿用于商业用途或大规模分发
- 请遵守 B站用户协议与相关法律法规,不要高频请求(项目已内置限速)
- 第三方接口随时可能变更或失效,`scripts/probe_api.py` 可用于快速定位问题
- 音频版权归原UP主与版权方所有,本项目不存储、不传播任何受版权保护的内容

---

## 参考项目

接口实现思路参考了以下开源项目(本项目为独立实现,未复制其代码):

- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) — 社区维护的接口文档
- [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api) — Python 版接口库
- [AprDeci/bili-music](https://github.com/AprDeci/bili-music) — 全平台 B站源音乐 App
- [bbplayer-app/BBPlayer](https://github.com/bbplayer-app/BBPlayer) — B站音乐播放器
- [bigbirdone/BiliBili_Music](https://github.com/bigbirdone/BiliBili_Music) — Flutter 实现的哔哔音乐

