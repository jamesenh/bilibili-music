//! 创建桌面应用依赖并注册Tauri命令，将启动接线集中在应用入口。

pub mod api;
pub mod cache;
pub mod client;
pub mod commands;
pub mod db;
pub mod models;
pub mod session;

use api::BiliApi;
use cache::AudioCache;
use client::BiliClient;
use commands::*;
use db::LibraryDb;

/// 初始化依赖、启动异步预热并运行Tauri桌面事件循环。
///
/// # Panics
/// 客户端、缓存或数据库初始化失败，或者桌面事件循环无法启动时终止进程。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let client = BiliClient::new().expect("Failed to create BiliClient");
    let api = BiliApi::new(client.clone());
    let cache = AudioCache::new().expect("Failed to initialize AudioCache");
    let db = LibraryDb::new().expect("Failed to initialize LibraryDb");

    let state = AppState {
        client: client.clone(),
        api,
        cache,
        db,
    };

    tauri::Builder::default()
        .manage(state)
        .setup(move |_app| {
            // 在 Tauri 异步运行时启动后执行预热任务
            let warmup_client = client;
            tauri::async_runtime::spawn(async move {
                let _ = warmup_client.warm_up().await;
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            window_minimize,
            window_toggle_maximize,
            window_close,
            window_is_maximized,
            search_videos,
            get_video_detail,
            resolve_and_cache_audio,
            record_play_history,
            get_play_history,
            clear_play_history,
            get_search_history,
            clear_search_history,
            get_cached_tracks,
            get_user_info,
            save_cookie,
            logout,
            get_session_path,
            get_user_fav_folders,
            get_fav_resources
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
