# BiliMusic的Rust/Tauri迁移

本目录是正在迁移中的桌面客户端，采用Tauri 2、Rust、Vue 3、TypeScript和Pinia。
Python/PySide6工程仍保留在仓库根目录，用于对照既有行为；迁移尚未完成，不能用旧版的测试结果代替新版验收。

## 代码结构

- `src/components/`：窗口、搜索、曲目列表、播放条、队列与账号弹窗。
- `src/services/tauriBridge.ts`：前端与Tauri命令的边界；浏览器环境回退到演示数据。
- `src/stores/player.ts`：播放与队列状态，通过HTML Audio播放缓存到本地的音频。
- `src-tauri/src/models.rs`：纯数据模型与标题处理。
- `src-tauri/src/client.rs`、`api.rs`：HTTP客户端与B站接口。
- `src-tauri/src/cache.rs`、`db.rs`、`session.rs`：音频缓存、本地sqlite库与会话存储。
- `src-tauri/src/commands.rs`：Tauri命令与流程编排；`lib.rs`负责启动与注册。

依赖方向为界面→桥接→Tauri命令→接口/存储→模型。纯模型与后续拆出的解析函数不得依赖界面或Tauri运行时。

## 本地运行

需要Node.js 22或更新的兼容版本、pnpm、Rust工具链。Windows桌面编译还需要Visual Studio C++ Build Tools及WebView2。
依赖版本以`pnpm-lock.yaml`、`src-tauri/Cargo.lock`为准，两个锁文件都要提交。

```bash
cd bili-music-tauri
pnpm install --frozen-lockfile
pnpm dev
```

浏览器预览默认地址为`http://localhost:1420`，使用演示数据和模拟播放进度，不能验证真实B站请求、账号或桌面音频播放。

```bash
# 启动真实桌面客户端
pnpm tauri dev

# 前端类型检查与生产构建
pnpm build

# Rust业务测试（不请求B站接口；首次可能下载构建依赖）
cargo test --locked --manifest-path src-tauri/Cargo.toml

# 构建桌面安装包
pnpm tauri build
```

## 云端继续开发

将本分支推送到GitHub后，为该仓库创建Codex云端环境，明确使用`codex/rust-migration`分支和本目录。
环境需要Node.js、pnpm、Rust，以及当前宿主平台所需的Tauri系统依赖；Linux环境不能用Windows构建成功的结果代替本平台检查。
先执行`pnpm install --frozen-lockfile`、`pnpm build`和上面的`cargo test`，再继续迁移。

只提交源码、配置、应用图标与锁文件，不提交`node_modules/`、`dist/`、`src-tauri/target/`或自动生成的schema。
不要上传真实Cookie、`session.json`、用户数据库或本地缓存。

## 当前缺口与后续验收

已接入搜索、详情/分P、音轨下载、缓存索引、播放/搜索历史、Cookie会话和只读收藏夹的初版代码。
这些是迁移中的实现入口，并不表示已完成端到端验收。继续开发时至少需要核对：

- Rust的snake_case字段与前端camelCase字段的序列化契约。
- 本地音频asset协议的启用与目录授权，以及真实播放、切歌取消和旧回调隔离。
- 网络预热等待、失败退避、缓存失败清理、并发写入与codec缓存键的行为对齐。
- Rust/Python目前共用应用数据目录；数据库schema兼容性未验收，测试必须注入沙箱路径，不能直接写真实用户数据。
- 会话文件在POSIX上的`0o600`权限、Windows原子替换与登出失败处理。
- 接口纯解析函数的拆分，以及数据库、缓存、账号和播放状态机的回归测试。

构建和纯函数单测不能代替真实GUI、音频输出与B站网络链路验证。Python的`scripts/smoke_test.py`只验证旧版。

## 本次迁移基线验证（2026-11-10，Windows）

- `pnpm install --frozen-lockfile`：通过。
- `pnpm build`：类型检查和生产构建通过。
- `cargo test --locked`（`src-tauri/`）：7个纯函数测试通过，无失败。
- `cargo fmt --check`（`src-tauri/`）：通过。
- 仓库根目录`.venv/Scripts/python.exe -m unittest discover -s tests -v`：
  `Ran 945 tests ... OK (skipped=3)`，3个跳过项是既有的POSIX权限测试。
  首轮发现的Windows数据库清理与路径分隔符问题已在测试资源收尾和等价路径断言中修正，未跳过或删除用例。

本次以保存迁移基线供云端继续开发为范围，未启动真实桌面播放、未请求B站接口，未制作安装包；
Python网络冒烟脚本未运行，因为它不能验证本目录的Rust实现。本次结果不构成迁移完成或完整功能验收。
