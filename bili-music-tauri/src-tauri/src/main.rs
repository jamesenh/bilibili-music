//! 启动桌面客户端，Windows发布构建隐藏额外控制台窗口。

// Windows发布构建隐藏额外控制台，保留桌面应用的窗口体验。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

/// 调用库入口启动桌面客户端。
fn main() {
    bili_music_lib::run();
}
