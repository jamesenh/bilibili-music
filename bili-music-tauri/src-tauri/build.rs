//! 生成Tauri构建上下文，保持平台资源与应用配置一致。

/// 根据tauri.conf.json生成桌面构建所需资源。
fn main() {
    tauri_build::build()
}
