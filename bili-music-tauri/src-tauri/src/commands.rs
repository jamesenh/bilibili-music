//! 暴露Tauri命令并编排接口与本地存储，模型和存储层不反向引用此模块。

use anyhow::Result;
use tauri::{State, Window};

use crate::api::BiliApi;
use crate::cache::AudioCache;
use crate::client::BiliClient;
use crate::db::LibraryDb;
use crate::models::{CachedTrackItem, FavFolder, PlayHistoryItem, UserInfo, Video};

/// 由Tauri托管的共享接口、缓存与数据库依赖。
pub struct AppState {
    pub client: BiliClient,
    pub api: BiliApi,
    pub cache: AudioCache,
    pub db: LibraryDb,
}

/// 最小化当前窗口
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn window_minimize(window: Window) -> Result<(), String> {
    window.minimize().map_err(|e| e.to_string())
}

/// 最大化或还原当前窗口
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn window_toggle_maximize(window: Window) -> Result<(), String> {
    if window.is_maximized().map_err(|e| e.to_string())? {
        window.unmaximize().map_err(|e| e.to_string())
    } else {
        window.maximize().map_err(|e| e.to_string())
    }
}

/// 关闭当前窗口
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn window_close(window: Window) -> Result<(), String> {
    window.close().map_err(|e| e.to_string())
}

/// 查询当前窗口是否处于最大化状态
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn window_is_maximized(window: Window) -> Result<bool, String> {
    window.is_maximized().map_err(|e| e.to_string())
}

/// 记录搜索关键词并返回指定页的视频结果。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn search_videos(
    state: State<'_, AppState>,
    keyword: String,
    page: i32,
) -> Result<Vec<Video>, String> {
    state
        .db
        .record_search(&keyword)
        .map_err(|e| e.to_string())?;
    state
        .api
        .search(&keyword, page)
        .await
        .map_err(|e| e.to_string())
}

/// 补全视频详情与分P列表，供点播时选择真实cid。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_video_detail(state: State<'_, AppState>, bvid: String) -> Result<Video, String> {
    state
        .api
        .get_video_detail(&bvid)
        .await
        .map_err(|e| e.to_string())
}

/// 按分P选择音轨并写入本地缓存，返回可播放的本地路径。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn resolve_and_cache_audio(
    state: State<'_, AppState>,
    video: Video,
    page_index: usize,
    quality_id: i32,
) -> Result<String, String> {
    let page = video
        .pages
        .get(page_index)
        .ok_or_else(|| "分P索引超出范围".to_string())?;

    // 1. 盲查本地缓存：若已有则零网络请求秒开播
    if state.cache.is_cached(&video.bvid, page.cid, quality_id) {
        let p = state
            .cache
            .get_cache_path(&video.bvid, page.cid, quality_id);
        return Ok(p.to_string_lossy().to_string());
    }

    // 2. 请求 playurl
    let tracks = state
        .api
        .get_audio_tracks(&video.bvid, page.cid)
        .await
        .map_err(|e| e.to_string())?;

    if tracks.is_empty() {
        return Err("该视频未提供有效音频流".to_string());
    }

    // 挑选匹配或最佳音轨
    let track = tracks
        .iter()
        .find(|t| t.id == quality_id)
        .unwrap_or(&tracks[0]);

    // 3. 流式原子下载到本地缓存
    let local_path = state
        .cache
        .download_and_cache(&track.base_url, &video.bvid, page.cid, track.id)
        .await
        .map_err(|e| e.to_string())?;

    let size = local_path.metadata().map(|m| m.len() as i64).unwrap_or(0);

    // 4. 索引落库
    let _ = state.db.record_cache_index(&CachedTrackItem {
        bvid: video.bvid.clone(),
        cid: page.cid,
        title: video.title.clone(),
        part: page.part.clone(),
        artist: video.owner_name.clone(),
        cover: video.pic.clone(),
        duration: page.duration,
        quality_id: track.id,
        size_bytes: size,
        cached_at: chrono_now_ms(),
        file_path: local_path.to_string_lossy().to_string(),
    });

    Ok(local_path.to_string_lossy().to_string())
}

/// 把前端报告的已播放分P写入最近播放记录。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn record_play_history(
    state: State<'_, AppState>,
    item: PlayHistoryItem,
) -> Result<(), String> {
    state
        .db
        .record_play_history(&item)
        .map_err(|e| e.to_string())
}

/// 读取按最近播放时间排序的本地记录。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_play_history(state: State<'_, AppState>) -> Result<Vec<PlayHistoryItem>, String> {
    state.db.get_play_history().map_err(|e| e.to_string())
}

/// 清空播放历史，保留音频缓存和搜索历史。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn clear_play_history(state: State<'_, AppState>) -> Result<(), String> {
    state.db.clear_play_history().map_err(|e| e.to_string())
}

/// 读取按最近搜索时间排序的关键词列表。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_search_history(state: State<'_, AppState>) -> Result<Vec<String>, String> {
    state.db.get_search_history().map_err(|e| e.to_string())
}

/// 清空关键词历史，保留播放记录与缓存。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn clear_search_history(state: State<'_, AppState>) -> Result<(), String> {
    state.db.clear_search_history().map_err(|e| e.to_string())
}

/// 读取本地缓存索引，供前端展示已缓存曲目。
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_cached_tracks(state: State<'_, AppState>) -> Result<Vec<CachedTrackItem>, String> {
    state.db.get_cached_tracks().map_err(|e| e.to_string())
}

/// 获取当前账号登录信息
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_user_info(state: State<'_, AppState>) -> Result<UserInfo, String> {
    if !state.client.has_cookie().await {
        return Ok(UserInfo {
            is_login: false,
            uname: String::new(),
            mid: 0,
            avatar: String::new(),
        });
    }

    state.api.get_user_info().await.map_err(|e| e.to_string())
}

/// 保存 Cookie 并校验登录有效性
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn save_cookie(
    state: State<'_, AppState>,
    cookie_str: String,
) -> Result<UserInfo, String> {
    if cookie_str.trim().is_empty() {
        return Err("Cookie 内容不能为空".to_string());
    }

    // 先存临时凭据，尝试调用 nav 校验有效性
    state
        .client
        .set_cookie(cookie_str.clone(), String::new(), 0)
        .await
        .map_err(|e| e.to_string())?;

    match state.api.get_user_info().await {
        Ok(info) if info.is_login => {
            // 校验成功，把真实的 uname 和 mid 更新进 session.json
            let _ = state
                .client
                .set_cookie(cookie_str, info.uname.clone(), info.mid)
                .await;
            Ok(info)
        }
        Ok(_) => {
            let _ = state.client.clear_cookie().await;
            Err("凭据无效或已过期，请重新获取完整 Cookie".to_string())
        }
        Err(e) => {
            let _ = state.client.clear_cookie().await;
            Err(format!("验证账号状态失败: {}", e))
        }
    }
}

/// 登出：清除内存与磁盘 session.json
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn logout(state: State<'_, AppState>) -> Result<(), String> {
    state.client.clear_cookie().await.map_err(|e| e.to_string())
}

/// 获取 session.json 的文件路径（供界面明示）
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_session_path(state: State<'_, AppState>) -> Result<String, String> {
    Ok(state.client.session_file_path())
}

/// 获取当前登录用户的收藏夹列表
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_user_fav_folders(state: State<'_, AppState>) -> Result<Vec<FavFolder>, String> {
    let user = state.api.get_user_info().await.map_err(|e| e.to_string())?;
    if !user.is_login || user.mid == 0 {
        return Ok(Vec::new());
    }
    state
        .api
        .get_fav_folders(user.mid)
        .await
        .map_err(|e| e.to_string())
}

/// 获取指定收藏夹内的视频列表
///
/// # Errors
/// 依赖调用失败或输入无法用于当前操作时，返回供界面处理的错误文本。
#[tauri::command]
pub async fn get_fav_resources(
    state: State<'_, AppState>,
    media_id: i64,
    page: i32,
) -> Result<Vec<Video>, String> {
    state
        .api
        .get_fav_resources(media_id, page)
        .await
        .map_err(|e| e.to_string())
}

/// 取得Unix毫秒时间戳，系统时钟早于纪元时返回零。
fn chrono_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
