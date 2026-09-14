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

# 账号链路只读验证(M5 S0):登录态 / 收藏夹接口 / 登录是否提升音质
# 只读、不落盘、不打印凭据;不带凭据时只出匿名基线
uv run python scripts/probe_login.py

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
│   ├── cache_index.py CacheIndex(已缓存音轨的索引,落在 library.db)
│   ├── library_db.py  LibraryDb(本地 sqlite 库:建表 + 读写)
│   ├── history.py     PlayHistory(最近播放:去重、倒序、按上限裁剪)
│   ├── download_task.py  DownloadTaskStore(批量缓存的任务与进度,跨重启恢复)
│   ├── queue.py       PlayQueue / PlayMode(队列与播放模式)
│   ├── session.py     Session / SessionStore(登录凭据的明文 JSON 落盘)
│   └── config.py      AppConfig / ConfigStore(纯 JSON 落盘)
├── net/             网络后端,两者接口一致可互换
│   ├── base.py        契约(Protocol)、重试策略、限速器、响应校验
│   ├── client.py      QtNetworkClient —— QNAM 实现(默认)
│   └── urllib_client.py  UrllibClient —— 标准库实现(对照/退路)
├── api/
│   └── bilibili.py    接口封装 + 纯解析函数(parse_*)
├── audio/
│   ├── resolver.py    AudioResolver —— 异步解析状态机(无线程)
│   ├── downloader.py  Downloader —— 批量缓存的串行调度(只下载、不播放)
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
        ├── sidebar.py       Sidebar(导航 + 收藏夹列表 + 刷新/显示隐藏 + 账号入口)
        ├── track_list.py    TrackList(搜索结果与队列共用的曲目列表)
        ├── player_bar.py    PlayerBar(传输控件 / 进度 / 分P / 音质 / 音量)
        ├── page_selector.py PageSelector(分P选择器与它的弹出菜单)
        ├── queue_drawer.py  QueueDrawer(右侧播放队列)
        ├── cache_page.py    CachePage(本地缓存页)
        ├── history_page.py  HistoryPage(最近播放页)
        ├── fav_page.py      FavPage(收藏夹页:失效条目标灰且点不动)
        ├── fav_visibility_dialog.py FavVisibilityDialog(挑哪些收藏夹显示在侧栏)
        ├── account_dialog.py AccountDialog(粘贴 Cookie 登录 / 查看账号 / 登出)
        ├── task_dialog.py   TaskDialog(下载任务:进度 / 暂停 / 继续 / 移除)
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

仍需登录 / WBI 签名的能力:

- 收藏夹列表与内容:**需要 `SESSDATA`**;是否需要 WBI 签名见下面「账号链路」一节
  (旧的"需 SESSDATA + WBI 签名"记载已更正)
- CC 字幕:匿名调用 `/x/player/v2` 时 `subtitles` **恒为空数组**

### 账号链路 S0 结果(2026-09-14,真账号,两轮)

用 `scripts/probe_login.py` 在**真账号**(大会员)上跑完两轮。**最大未知量已经问掉:
收藏夹接口不需要 WBI 签名** —— 所以按计划要做的 `core/wbi.py` **不做**(除非后续遇到
别的接口)。

| S0 问题 | 结论 | 对 S1 的影响 |
|---|---|---|
| 收藏夹接口要不要 `w_rid`/`wts` | **不需要**。16 个收藏夹列表与内容(20 条/页)**全程不带签名**都拿到了 `code=0` | 砍掉 `core/wbi.py`;但限速与"像浏览器"仍然要做 |
| 私密收藏夹能不能读 | 能。多数夹子 `attr=22`(私密),`默认收藏夹` 是 `attr=0` | S1 必须显示私密标记,且**不能假设匿名也能看到**(匿名拿到的 `data=null`) |
| 条目里有没有分P `cid` | **没有**。字段是 `id/type/title/cover/page/duration/upper/attr/bvid/season/...` | 多P条目必须补 `pagelist`(每条一次请求,限速下的主要成本) |
| `ps` 上限与翻页 | `ps=20` 足额返回 20 条,`has_more=True`,`info.media_count=128` | 分页按 `has_more` 走,别用"空页"判结束 |
| 条目 `type` 取值 | 本页 20 条**全是 2**(视频);文档里 12=音频、21=合集,两轮都没碰上 | S1 先只处理 `type=2`,`12`/`21` 跳过并标注原因 |
| 失效条目 | 一页 20 条里 **6 条 `attr≠0`(30%)** | S1 必须把失效条目标灰、禁止点击,否则一点就报错 |
| `pagelist` 拿分P | 100P 的合集:`条目 page=100` 与 `pagelist` 长度**完全一致**,第 1P 的 `cid` 与自己的 `duration` 都拿到了 | 补 `cid` 这条路可行 |
| 失效条目怎么认 | `pagelist` 与 `view` **一起失败**:两条分别是 `attr=1` + `view -404`、`attr=9` + `view 62002`;**正常条目 `attr=0` 两边都成功** | **S1 只看条目自带的 `attr` 就能过滤失效条目,零额外请求**;`62002` = 稿件不可见 |
| 登录能否换来更高音频档位 | **未能观测到增益**:5 条正常视频,匿名与登录都是 `[30216, 30232, 30280]`、`flac/dolby` 全空。**匿名本来就已拿到 30280(常规最高档)**,会员档位(30250 杜比 / 30251 Hi-Res)取决于**稿件本身有没有那条音轨** —— 这 5 条投稿都没有 | **不是 S1 的阻塞项**:登录是收藏夹的前置件,M5 的价值来自"收藏夹当歌单"。要不要支持会员档位归 S2,且先得找到一条真有 Hi-Res/杜比的视频来验 |
| 登录态风控节奏 | 登录臂 14 次 + 匿名臂 10 次请求,**风控码 0 次**(业务码只有 `-404`/`62002`,属失效条目而非风控) | 小批量安全;S1 逐条补 `pagelist` 时必须继续限速 |
| `cookie/info` | `refresh=False`(两轮都不需要刷新) | 保鲜机制可以先不做,但要在设置里显示"凭据可能过期" |

**仍未验证**:结论清单里明列的"扫码成功时的凭据形态""扫码轮询的 `86090`""Cookie 保鲜
三段式"(都排进 S1/S2);以及"登录能否拿到会员音质"—— 这需要一条本身就带 Hi-Res/杜比
音轨的视频才能定案,用 `scripts/probe_login.py --bvid BV...` 可单独验。

### 账号链路:匿名实测(2026-09-14)

| 探测项 | 2026-09-14 匿名实测 | 设计含义 |
|---|---|---|
| `passport.../qrcode/generate` | `code=0`,返回 `url` + `qrcode_key` | 扫码入口不需要登录态 |
| `.../qrcode/poll` | **外层 `code` 恒为 0**,状态在内层 `data.code`:假 key=`86038 已失效`、真 key 未扫码=`86101 未扫码` | 现有 `check_payload` 只看外层码,扫码逻辑必须单独解析内层 |
| `/x/v3/fav/folder/created/list-all` | 匿名 `code=0` 但 **`data=null`**(换 5 个 mid 都一样;分页版是 `count=0/list=0`) | **不能靠它判断登录是否失效** —— 它不报错、只静默给空,登录态判定必须用 `nav.isLogin` |
| `/x/web-interface/nav` | 匿名 `code=-101`,但 `data.wbi_img` 里有密钥 | WBI 密钥不需要登录就能拿 |
| `playurl` + 伪造 SESSDATA | `code=0`,音轨与匿名完全相同(3 条) | 失效登录态**不会拖垮**匿名链路,可以优雅降级 |
| `/x/player/playurl` 与 `/x/player/wbi/playurl` | **两条路径都不带 `w_rid`/`wts`** 也返回 `code=0`、3 条音轨 | 至少音频这一路目前没强制 WBI(与文档标注的"playurl 需 Wbi 签名"不一致) |
| `/x/space/wbi/acc/info`(已知需签名的对照组) | 匿名 `-403`,另一轮变 `-352`;**本地实现签名后仍 `-352`** | **WBI 实现无法在匿名条件下验证**,必须真账号 |
| `QNetworkCookieJar.toRawForm()` / `parseCookies()` | 往返保真(含 domain/secure/expires),过期 Cookie 会被 jar 拒收 | 登录态持久化**零新依赖**可行,而且天然挡住"复活过期凭据" |
| PySide6 6.8.3 的二维码能力 | **没有 QR 编码 API**;venv 里也没有 `qrcode` / `segno` / `PIL` | 扫码登录要么新增依赖,要么在 `core/` 手写 QR 编码器 |
| 主页预热 | 偶尔**不下发** `buvid3`(同一轮里先跑的臂没拿到、后跑的拿到了) | 预热"请求成功"不等于"拿到了 Cookie",诊断要分开判断 |

**关于旧记载的更正**:本节曾写"收藏夹列表与内容(需 `SESSDATA` + WBI 签名 `w_rid`/`wts`)"。
社区文档口径其实只要求 Cookie(`SESSDATA`),**没有要求 WBI** —— 把文档里全部 198 个 md
按 `鉴权方式:[Wbi 签名]` grep,命中的是评论、私信、搜索、用户信息、用户空间、playurl、
AI 总结与直播信息流,**一个收藏夹接口都没有**;真账号实测也确认了这一点。但
"不需要签名 ≠ 可以随便请求":`-352` 的官方定义是"UA **或** wbi 参数不合法"。

> 社区权威接口文档仓库 `SocialSisterYi/bilibili-API-collect` 已被作者关停(GitHub API
> 实测 `archived=true`、`default_branch=deprecated`、最后推送 `2026-01-30`)。此后的接口
> 变更不再有社区权威记录 —— 这也是本节的实测结论必须自己沉淀的原因。

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

**搜索接口里没有任何"分P"信息。** 实测 `search/type` 与 `search/all/v2` 的视频条目都是同样
66 个字段,没有分P数量、也没有"是否多P"的标志(`episode_count_text` 恒为空串,`biz_data`
为 `null`,`is_union_video` 恒为 0)—— 一条 150P 的合集与一条单P视频在搜索响应里长得一样。
条目里的 `duration` 是**视频级总时长**(= 所有分P之和),但**不能拿它反推分P数**:实测有单P
视频长达 8.5 小时(513:50)。所以列表里的"分P"列在详情接口回来之前只能显示 `?`,要提前显示
就必须为每一行额外打一次 `view` 接口。

### 搜索结果分页的实测行为(2026-09)

`page_size` 显式传 30 能拿到 30 条(`numResults` 封顶 1000,`numPages` = `ceil(numResults /
pagesize)`,所以它随接口**实际**使用的 pagesize 变化)。但翻页有三个坑:

1. **相邻两页会重叠**:实测同一会话里 `page=1` 与 `page=2` 有 3 条重复 —— 两次请求之间排序
   会变。所以追加结果**必须按 `bvid` 去重**,否则列表里会出现重复行。
2. **排序本身不稳定**:同会话里连打两次 `page=1`,尾部条目不同。
3. **越界页码被静默当成第 1 页**:`page=1000` 返回的是 `data.page=1` 且内容与第 1 页相同。
   因此**不能用"返回空页"当结束条件**,否则会永远重复拉第 1 页;要同时看 `numPages`、
   响应里的 `page` 是否与请求一致,以及本页有没有带来新条目(`main_window._decide_has_more`)。

另有一次 `page=1&page_size=30` 只回了 20 条(`pagesize=20`),重跑未复现 —— 属于偶发,所以
实现里也不能假设"返回条数恰好等于 `page_size`"。

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

### 本地库(`library.db`):缓存索引与最近播放

缓存文件名是 `(bvid, cid, 音质, codec)` 的 SHA-1 前 20 位,只能"知道键再问文件在不在";
而"本地缓存"页要反向枚举出曲名、UP主、音质与体积。所以解析成功时会把这一条音轨的
元数据写进本地库(配置目录下的 `library.db`):

```sql
CREATE TABLE cached_tracks (
    file_name TEXT PRIMARY KEY,   -- 音频文件名(缓存目录下)
    bvid TEXT, cid INTEGER, quality_id INTEGER, codec TEXT, bandwidth INTEGER,
    title TEXT, author TEXT, page_index INTEGER, page_title TEXT, multipart INTEGER,
    duration INTEGER, cover_url TEXT, size_bytes INTEGER, cached_at REAL
);

CREATE TABLE play_history (       -- 「最近播放」,(bvid, cid) 是主键 => 同一首只留最近一次
    bvid TEXT, cid INTEGER, page_index INTEGER, title TEXT, author TEXT,
    page_title TEXT, multipart INTEGER, duration INTEGER, cover_url TEXT,
    played_at REAL, PRIMARY KEY (bvid, cid)
);

CREATE TABLE download_tasks (     -- 「缓存整个合集」的任务,bvid 是主键 => 一个视频只有一条任务
    bvid TEXT PRIMARY KEY, title TEXT, author TEXT, cover_url TEXT,
    total_pages INTEGER, done_pages INTEGER, bytes_done INTEGER, page_index INTEGER,
    state TEXT, error TEXT, created_at REAL, updated_at REAL
);
```

为什么是 sqlite 而不是 JSON(实现见 `core/library_db.py`):

- **增删是一行的事**:播放历史是"每开始播一首都写一条"的高频写入,JSON 方案每写一条
  都要整份重写 + 原子替换,撑不住。
- **一个库多张表**:缓存索引、播放历史与下载任务同库,但它们**互不牵连** —— "清空缓存"
  只删 `cached_tracks` 与磁盘上的音频,"清空历史"只删 `play_history`,"清空已完成的任务"
  只删 `download_tasks`,三者都不会顺手把对方带走(有用例钉着这三条隔离)。
- **库放配置目录**，不放缓存目录：音频放 `%LOCALAPPDATA%`(随时可以整个删掉，所以
  记录指向不存在的文件是常态，靠 `prune()` 剪掉)，库与 `config.json` 同属用户数据，
  该跟着漫游。
- **`sqlite3` 是标准库**，不新增依赖；它不依赖事件循环，于是 `core` 仍然零 Qt 引用。
- **开 WAL**，崩溃安全交给 sqlite 自己的日志，不再需要 `先写 .part 再 os.replace`。
- **旧版 `index.json` 不迁移、直接删**：索引只是磁盘快照，没存不可再生的信息。
  代价是**在索引出现之前缓存下来的非常规档位(如 `fLaC`)会变成僵尸文件** ——
  盲查只覆盖 30280 / 30232 / 30216 三档，那些歌要重新缓存一次才会重新出现在缓存页上。

设计要点:

- **索引可再生 / 历史不可再生**:索引读坏、丢失、被手删，都会退回按档位盲查
  (`pick_best_cached`)，绝不影响播放；两者写入失败都只当这次记录作废，
  不会让一次已经成功的播放变成失败；库开不起来时界面显示空列表，不弹窗。
- **`file_name` 只接受纯文件名**(含反斜杠一律拒绝):库文件可以被手改，这一条能挡住
  `..\..\x.m4a` 这类路径穿越 —— 否则一次"删除缓存"会删到缓存目录之外。
- **重复播放不刷新缓存时间**:`cached_at` 记的是"这首歌什么时候存下来的"，
  内容没变时连写库都跳过(命中缓存的那条路径同样会调用它，所以不会重复写)。
- **索引里有不等于文件还在**:命中索引后仍要 `stat` 一次，文件被手删的记录会在下次
  打开缓存页时被剪掉。

### 最近播放

* 写入时机:**音频真正就绪**时(`audio_ready`)才记一条 —— 解析失败、快速切歌那些
  "其实没听"的项不会留痕迹；一次播放只写一条，播放位置那种高频信号不参与。
* 粒度是**分P**(音乐区一个分P就是一首歌)，所以同一个合集的不同 P 是两条记录。
* `(bvid, cid)` 是主键:重复播放把那一行提到最前并刷新时间，不多出一行。
* 条数上限 `history_limit` 默认 200，写在 `config.json` 里、**可手改**（不做设置界面）；
  超出后按播放时间从旧到新丢掉。
* 时间列用相对时间(`刚刚` / `12 分钟前` / `昨天 21:30` / `9月1日` / `2025年3月1日`)，
  跨天按**本地日历日**判断，不是"满 24 小时"。
* 双击 = 眼前这张列表整列变成队列；已缓存的歌点播**零网络请求**，没缓存的照常解析下载。
* "从历史中删除" / "清空历史"都**只删记录**，不动磁盘上的音频与缓存索引。

### 批量缓存(一键缓存整个合集)

搜索结果里右键一个视频,选「缓存到本地 / 缓存全部 N 个分P」即可把整个合集存下来。
实现在 `audio/downloader.py`,与播放**互不干扰**:

* **为什么不复用播放用的解析器**:`AudioResolver` 是单任务状态机,再调一次 `resolve()`
  就会取消上一个。共用的话,用户一点播放就把缓存任务掐死了。所以批量缓存走自己的一条
  管线(只下载、不播放),两者共用同一个 `AudioCache`(原子写入)与同一个限速器。
* **任务之间串行、分P之间也串行**:同一时刻只有一个任务在下、一个分P在下 ——
  这是刻意的"避免风控/限流",不要为了提速把它们并发起来。
* **已缓存的分P直接跳过**:任务开始时按缓存索引(不是任务记录)现算"还差哪几P",
  所以暂停过、重启过、或者那些歌本来就是播放时顺手缓存下来的,进度都对得上。
* **失败挂起整条队列**:失败绝大多数是风控(412/429),继续把排队的任务一个个发出去
  只会把惩罚拖长。所以当前任务置为失败、排队中的任务一并转为暂停,并弹窗告知。
* **暂停丢半截文件**:恢复时那一P从头下 —— **不做字节级续传**,因为音频 CDN 是否接受
  `Range` 尚未实测(路线图 M2.5);没结论之前不假装支持。
* **正在播放的那一P推迟一轮**:播放器自己正在写同一个缓存文件,两条管线同时写一个
  `.part` 会写出坏文件。推迟到本轮末尾再试一次;仍然在播就如实说"还有 N 个未缓存"。
* **任务跨重启保留**:`download_tasks` 只记进度数字(分P计数 / 字节数 / 当前分P),
  不记分P明细;重启后未完成的任务一律是**暂停**状态,由用户点"继续"才接着下。
* 界面:本地缓存页头部有「下载任务 (N)」与「打开缓存目录」两个按钮;任务对话框(非模态)
  每行显示进度并能单独暂停/继续/移除,顶部可全部暂停/继续/清空已完成。

---

## 已实现的功能

- [x] **自绘标题栏的无边框窗口**:应用图标 + 圆角搜索框 + 粉色搜索按钮 + 最小化/最大化/关闭,
      边缘可拖动缩放(缩放手势交给平台处理,吸附与多屏表现与原生窗口一致)
- [x] **左侧导航栏**:搜索结果页 / 最近播放页 / 本地缓存页都是真的;"发现"与"我的歌单"
      给出**说明清楚的占位页**(功能分别属于路线图 M3 / M5,还没有实现)
- [x] 关键字搜索视频(**结果列表带封面缩略图**,含标题/UP主/时长/分P数/播放量)
- [x] **搜索结果分页加载**:滚到接近底部自动取下一页并追加(按 `bvid` 去重,最多 5 页 ≈ 150 条),
      翻页失败时底部出现"重试"按钮(滚动触发是隐式的,失败必须给看得见的出口)
- [x] 自动识别多P合集(**每 P 即一首歌**)
- [x] **播完自动跳到下一个分P**(多P合集时相当于自动下一首)
- [x] **播放条上的分P选择器**:菜单锚在选择器上方列出当前视频的每个分P(编号 / 标题 / 该P自己的时长),
      选中即切到该分P(按该分P的 `cid` 解析与缓存);单P视频或没有当前视频时禁用
- [x] **播放队列**:双击搜索结果=整个结果成为队列并从该行开始播;行内"+"直接入队;
      右键可"下一首播放 / 加入队列"
- [x] **右侧播放队列面板**:待播列表带封面、高亮当前项、双击跳转、右键移除、一键清空;
      显示/隐藏在播放条上有一个开关(侧栏不再放入口)
- [x] **四种播放模式**:顺序 / 列表循环 / 单曲循环 / 随机(模式会被记住)
- [x] **手动切歌**:上一首 / 下一首 / 播放暂停按钮
- [x] **手动切换音质**(自动 / 192K / 132K / 64K),切换后**保留播放位置**
- [x] 音质自动选择(默认最高码率),界面显示实际音质与码率
- [x] 异步下载,界面全程不阻塞,带进度条
- [x] 音频磁盘缓存(原子写入,二次播放**零请求**直接开播)
- [x] **缓存索引**(配置目录下的 `library.db`,`cached_tracks` 表):记下每首已缓存音轨的
      曲名 / UP主 / 音质 / 时长 / 体积,让缓存**可枚举**。索引读坏或丢失只会退回按档位盲查,不影响播放
- [x] **本地缓存页**:列出已缓存的歌(封面 + 曲名 / 音质 / 时长 / 体积,最近缓存的在前),
      按曲名或 UP主过滤;双击 = 把当前列表变成队列并**离线播放**(零网络请求),
      行内 "+" 入队,右键可"从缓存删除",右上角一键清空(删除前确认,清不掉的会说明原因)
- [x] **最近播放页**(`play_history` 表):音频真正就绪时记一条(同一首只留最近一次,
      上限 200、可在 `config.json` 里手改),按时间倒序、按曲名/UP主过滤、行尾显示相对时间;
      双击回放(已缓存的零网络请求),右键可"从历史中删除",右上角清空(都只删记录、不动缓存)
- [x] **一键缓存整个合集**(路线图 M2.4):搜索结果里右键「缓存到本地 / 缓存全部 N 个分P」,
      确认后按分P顺序**串行**缓存(与播放互不干扰),已缓存的分P自动跳过
- [x] **下载任务对话框**(`download_tasks` 表):每行显示"已完成 3 / 共 200 个分P"、
      当前分P百分比与已下载体积,可单独暂停/继续/移除,顶部可全部暂停/继续/清空已完成;
      任务**跨重启保留**(未完成任务重启后是暂停态),失败时挂起整条队列并弹窗告知
- [x] **打开缓存目录**:本地缓存页头部一键用系统文件管理器打开缓存根目录
- [x] 详情接口内存缓存(减少请求,降低风控风险)
- [x] 播放 / 暂停 / 进度拖拽 / 音量 / 播放结束回调
- [x] **封面显示**(播放条与列表行,内存缓存,失败静默退回占位图);
      列表封面**一次只取一张**,避免把几十个请求一起甩给 CDN
- [x] **深色单主题**:近黑底 + B站粉强调色,`QPalette` 与全局样式表一起上(设计稿即深色,不做浅色变体)
- [x] 音量、播放模式、上次播放位置与「我的歌单」里被隐藏的收藏夹持久化到配置文件
      (`%APPDATA%\BiliMusic\config.json`);缓存索引与最近播放放在同目录的 `library.db`
      (sqlite,标准库)
- [x] 两种网络后端可互换(Qt / urllib)
- [x] **登录(粘贴 Cookie)与收藏夹当歌单**(路线图 M5 S1):侧栏「我的歌单」就是 B站
      收藏夹(带内容条数;私密夹在页面标题行标出);点一个就列出里面的视频,可过滤、
      翻页(每页 20 条,显式的「加载更多」按钮)、双击播放、行内入队、右键菜单
      (播放 / 下一首播放 / 加入队列 / 在B站打开)。**失效条目标灰且点不动**,点了只说原因
- [x] **收藏夹列表的本地操作**:侧栏「我的歌单」上的「刷新」重新从 B站 取一遍列表
      (在网页上新建 / 删除收藏夹后本机不会自己知道);「显示/隐藏」打开弹窗勾选哪些
      收藏夹列进侧栏。隐藏是**纯本机偏好**(只影响本机侧栏,B站 上的收藏夹不会被改),
      落 `config.json` 的 `fav_hidden_ids`
- [x] **账号对话框**:从浏览器复制 Cookie 整行即可登录(输入框遮蔽取值、**明示凭据保存
      路径**、一键登出会删掉凭据文件);启动时恢复上次登录态并用 `nav` 校验一次

## 尚未实现

- [ ] **M5 账号能力的剩余部分**:扫码登录(要定二维码方案:加依赖 / 手写 `core/qr.py`)、
      Cookie 保鲜(三段式,需自己实现 RSA-OAEP)、登录后的会员音质(仍未观测到增益)、
      收藏夹写回(收藏 / 取消收藏,属写回类操作,不在当前授权内)。
      分阶段计划见 `docs/ROADMAP.md`;S0 验证脚本 `scripts/probe_login.py`
- [ ] WBI 签名(`w_rid` + `wts`):真账号实测**不需要**(收藏夹与音频 playurl 都不带签名
      也能拿到 `code=0`),所以 `core/wbi.py` 没有做
- [ ] 歌词:目前匿名拿不到 CC 字幕;可考虑从视频弹幕或第三方歌词库补齐
- [ ] "发现"(音乐区排行榜)页面:侧栏已留入口,点开是占位页
- [ ] 本地曲库的进阶能力:断点续传(路线图 M2.5,需先实测音频 CDN 是否接受 `Range`)
- [ ] 歌单(含"喜欢"按钮):侧栏已留分组,点开是占位页- [ ] 全屏播放(播放条右下角的按钮当前**禁用**,没有假装能用)
- [ ] 上次播放位置**续播**:配置里已经在记录,但启动时不会自动接着放
      (开机自动出声比较打扰,要不要做得先定;见 `docs/ROADMAP.md`)
- [ ] 队列跨重启恢复(当前只记播放模式与位置,不记住队列内容)
- [ ] 打包分发(PyInstaller / Nuitka)

> 刻意**不做**的:缓存任务并发(串行是为了避免风控)、下载速度与剩余时间估算
> (要靠滑动平均估,估不准反而添乱)、开下之前先跑一遍 playurl 去算总量(那要几百次请求)。

> 上面这些的排期、依赖关系与下一步要做什么,见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。
> 该文档还记录了已拍板的决策(当前阶段不碰登录/WBI/收藏夹)与**尚未实测的技术假设**。

---

## 合规提示

本项目通过非官方接口访问 B站数据,处于**灰色地带**:

- 仅供**个人学习与技术研究**使用,请勿用于商业用途或大规模分发
- 请遵守 B站用户协议与相关法律法规,不要高频请求(项目已内置限速)
- 第三方接口随时可能变更或失效,`scripts/probe_api.py` 可用于快速定位问题
- 音频版权归原UP主与版权方所有,本项目不存储、不传播任何受版权保护的内容
- **账号能力(M5)额外风险**:登录态打的是"带真实账号"的非官方接口,风控代价落在**账号**
  而不是 IP 上(`-352` 风控校验失败 / `-412` 请求被拦截 / `-799` 请求过于频繁 /
  `-102` 账号被封停都是接口文档里定义过的码)。验证与使用**建议只用小号或可弃账号**;
  `scripts/probe_login.py` 全程只读、不落盘、不打印凭据取值
- **凭据以明文保存(用户 2026-09-14 明确选择的方案)**:登录凭据(`SESSDATA` 等)写在
  `%APPDATA%\BiliMusic\session.json`(与 `config.json` 同目录)。POSIX 下权限收紧到
  `0o600`,Windows 上 `%APPDATA%` 本身是每用户目录。**任何能读到这个文件的程序都能接管
  该账号** —— 不打算继续用时请用应用内的"登出"(它会删掉这个文件)。要换成系统加密
  (如 Windows DPAPI),只需改 `core/session.py` 的 `SessionStore.save/load` 两处
- **社区权威文档已关停**:`SocialSisterYi/bilibili-API-collect` 自 2026-01-30 起
  `archived` + `default_branch=deprecated`(GitHub API 实测)。此后接口变更不再有社区
  权威记录,一切以本仓库「B站接口实测笔记」里的自测结论为准

---

## 参考项目

接口实现思路参考了以下开源项目(本项目为独立实现,未复制其代码):

- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) — 社区维护的接口文档
- [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api) — Python 版接口库
- [AprDeci/bili-music](https://github.com/AprDeci/bili-music) — 全平台 B站源音乐 App
- [bbplayer-app/BBPlayer](https://github.com/bbplayer-app/BBPlayer) — B站音乐播放器
- [bigbirdone/BiliBili_Music](https://github.com/bigbirdone/BiliBili_Music) — Flutter 实现的哔哔音乐

